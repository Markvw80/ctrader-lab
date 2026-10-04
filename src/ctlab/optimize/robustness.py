"""Overfitting checks: parameter sensitivity and the final robustness verdict."""

import statistics
from collections.abc import Callable

from ctlab.optimize.objective import REJECTED
from ctlab.strategy.base import Strategy
from ctlab.strategy.params import Param


def neighbours(p: Param, value) -> list:
    """Values next to `value`: one step and ~10% of the range on each side (within bounds)."""
    if p.kind == "choice":
        return [c for c in p.choices if c != value]
    span = p.high - p.low
    deltas = sorted({p.step or 0, 0.1 * span} - {0}) or [0.1 * span]
    out = []
    for d in deltas:
        for sign in (-1, 1):
            v = value + sign * d
            if p.step:
                v = p.low + round((v - p.low) / p.step) * p.step
            v = min(max(v, p.low), p.high)
            v = round(v) if p.kind == "int" else round(v, 10)
            if v != value and v not in out:
                out.append(v)
    return out


def sensitivity(strategy_cls: type[Strategy], best: dict,
                score: Callable[[dict], tuple[float, dict]], min_stability: float) -> dict:
    """Re-run with each parameter moved slightly. A robust optimum sits on a plateau."""
    base_value, base_m = score(best)
    rows = []
    for name, spec in strategy_cls.param_specs().items():
        for v in neighbours(spec, best[name]):
            value, m = score({**best, name: v})
            rows.append({"param": name, "value": str(v), "objective": value,
                         "net_profit": m["net_profit"], "trades": m["trades"],
                         "rejected": value == REJECTED})
    valid = [r["objective"] for r in rows if not r["rejected"]]
    median = statistics.median(valid) if valid else None
    stability = (median / base_value) if (median is not None and base_value > 0) else None
    profitable = sum(1 for r in rows if r["net_profit"] > 0) / len(rows) if rows else 0.0
    reasons = []
    if base_value <= 0 or base_value == REJECTED:
        reasons.append("best parameters are not profitable/valid on this period")
    elif stability is None or stability < min_stability:
        reasons.append(f"objective drops to {stability if stability is None else round(stability, 2)}x "
                       f"of the optimum when parameters shift slightly (min {min_stability})")
    if base_m["net_profit"] > 0 and profitable < 0.75:
        reasons.append(f"only {profitable:.0%} of nearby parameter sets are profitable")
    rejected = sum(r["rejected"] for r in rows)
    if rows and rejected / len(rows) > 0.25:
        reasons.append(f"{rejected}/{len(rows)} nearby parameter sets have too few trades")
    worst = sorted(rows, key=lambda r: r["objective"])[:3]
    return {"base_objective": base_value, "median_neighbour_objective": median,
            "stability": stability, "profitable_neighbours_pct": round(profitable * 100, 1),
            "sensitive": bool(reasons), "reasons": reasons, "rows": rows,
            "worst": [f"{r['param']}={r['value']}: "
                      + ("rejected (too few trades)" if r["rejected"] else f"{r['objective']:.3f}")
                      for r in worst]}


def verdict(is_annual: float, oos_annual: float, windows: list[dict], oos_trades: int,
            sens: dict | None, s: dict) -> dict:
    """Explicit robustness label. Any reason -> NOT ROBUST."""
    reasons = []
    n = len(windows)
    min_oos = s["min_trades_oos"]
    conclusive = [w for w in windows if (w["oos_trades"] or 0) >= min_oos and not w["is_rejected"]]
    if n == 0:
        reasons.append("no walk-forward windows (not enough data)")
    if oos_trades < min_oos * max(n, 1):
        reasons.append(f"too few out-of-sample trades ({oos_trades} < {min_oos} per window)")
    if any(w["is_rejected"] for w in windows):
        k = sum(w["is_rejected"] for w in windows)
        reasons.append(f"{k}/{n} windows found no valid parameters in-sample (min trades)")
    losing_is = [w for w in windows if not w["is_rejected"] and (w["is_net"] or 0) <= 0]
    if losing_is:
        reasons.append(f"{len(losing_is)}/{n} windows: even the best in-sample parameters lose money")
    if oos_annual <= 0:
        reasons.append(f"out-of-sample is losing ({oos_annual:.2f}% per year)")
    elif is_annual > 0 and oos_annual < s["not_robust_oos_is_ratio"] * is_annual:
        reasons.append(f"out-of-sample return {oos_annual:.2f}%/yr is < "
                       f"{s['not_robust_oos_is_ratio']:.0%} of in-sample {is_annual:.2f}%/yr")
    if conclusive:
        prof = sum(1 for w in conclusive if w["oos_net"] > 0) / len(conclusive) * 100
        if prof < s["min_profitable_oos_windows_pct"]:
            reasons.append(f"only {prof:.0f}% of out-of-sample windows profitable")
    if sens and sens["sensitive"]:
        reasons.extend(f"sensitivity: {r}" for r in sens["reasons"])
    ratio = (oos_annual / is_annual) if is_annual > 0 else None
    return {"robust": not reasons, "label": "ROBUST" if not reasons else "NOT ROBUST",
            "reasons": reasons, "oos_is_ratio": None if ratio is None else round(ratio, 3)}
