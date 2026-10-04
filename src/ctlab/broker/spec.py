"""Broker symbol spec (from ProtoOASymbol) and spread calibration, stored under data/broker/.

These values override the defaults in config/symbols/<SYMBOL>.yaml (see ctlab.config.symbol_spec).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from ctlab.config import env
from ctlab.data.timezones import NY

COMMISSION_TYPES = {1: "usd_per_million_notional", 2: "usd_per_lot",
                    3: "percentage_of_value", 4: "quote_ccy_per_lot"}
SWAP_TYPES = {0: "pips", 1: "percentage"}
WEEKDAYS = {1: "monday", 2: "tuesday", 3: "wednesday", 4: "thursday", 5: "friday",
            6: "saturday", 7: "sunday"}


def broker_path(symbol: str) -> Path:
    return env().ctlab_data_dir / "broker" / f"{symbol.upper()}.json"


def load_overrides(symbol: str) -> dict:
    p = broker_path(symbol)
    return json.loads(p.read_text()) if p.exists() else {}


def save_overrides(symbol: str, update: dict) -> Path:
    p = broker_path(symbol)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = _deep_merge(load_overrides(symbol), update)
    p.write_text(json.dumps(data, indent=2, default=str))
    return p


def symbol_overrides(sym) -> dict:
    """ProtoOASymbol -> overrides in the same shape as config/symbols/<SYMBOL>.yaml."""
    lot_cents = sym.lotSize or 0
    contract = lot_cents / 100 if lot_cents else None
    ctype = COMMISSION_TYPES.get(sym.commissionType, f"unknown_{sym.commissionType}")
    if sym.preciseTradingCommissionRate:
        scale = 1e5 if ctype == "percentage_of_value" else 1e8
        commission = sym.preciseTradingCommissionRate / scale
    else:
        commission = sym.commission / 100  # legacy field, in cents
    out = {
        "symbol_id": sym.symbolId,
        "digits": sym.digits,
        "tick_size": 10 ** -sym.digits,
        "pip_size": 10 ** -sym.pipPosition,
        "costs": {
            "commission": {"type": ctype, "value_per_side": commission},
            "swap": {
                "calculation": SWAP_TYPES.get(sym.swapCalculationType, str(sym.swapCalculationType)),
                "long": sym.swapLong,
                "short": sym.swapShort,
                "triple_day": WEEKDAYS.get(sym.swapRollover3Days, "wednesday"),
            },
        },
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if contract:
        out["contract_size"] = contract
        out["min_lot"] = sym.minVolume / lot_cents
        out["lot_step"] = sym.stepVolume / lot_cents
    return out


def spread_profile(quotes: pl.DataFrame) -> dict:
    """Spread per New York local hour (the rollover is always 17:00 NY, regardless of DST).

    Per minute the median quoted spread is taken; per hour the median and p90 of those minutes.
    """
    per_minute = (
        quotes.with_columns(spread=pl.col("ask") - pl.col("bid"))
        .group_by(minute=pl.col("ts").dt.truncate("1m"))
        .agg(pl.col("spread").median())
    )
    per_hour = (
        per_minute.group_by(hour=pl.col("minute").dt.convert_time_zone(NY).dt.hour())
        .agg(median=pl.col("spread").median(), p90=pl.col("spread").quantile(0.9), minutes=pl.len())
        .sort("hour")
    )
    return {
        "model": "hourly_ny",
        "tz": NY,
        "hours": {int(r["hour"]): {"median": round(r["median"], 5), "p90": round(r["p90"], 5),
                                   "minutes": r["minutes"]}
                  for r in per_hour.iter_rows(named=True)},
        "overall_median": round(float(per_minute["spread"].median()), 5),
        "calibrated_from": [str(quotes["ts"].min()), str(quotes["ts"].max())],
    }


def merge_profiles(profiles: list[dict]) -> dict:
    """Combine per-day hourly profiles, weighting each hour by its number of minutes."""
    hours: dict[int, list] = {}
    for p in profiles:
        for h, v in p["hours"].items():
            hours.setdefault(int(h), []).append(v)
    merged = {}
    for h, vs in sorted(hours.items()):
        w = sum(v["minutes"] for v in vs)
        merged[h] = {k: round(sum(v[k] * v["minutes"] for v in vs) / w, 5) for k in ("median", "p90")}
        merged[h]["minutes"] = w
    froms = [p["calibrated_from"] for p in profiles]
    return {
        "model": "hourly_ny", "tz": NY, "hours": merged,
        "overall_median": round(sorted(p["overall_median"] for p in profiles)[len(profiles) // 2], 5),
        "calibrated_from": [min(f[0] for f in froms), max(f[1] for f in froms)],
    }


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out
