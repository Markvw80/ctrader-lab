"""Performance metrics and breakdowns from a BacktestResult."""

import math

import polars as pl

from ctlab.engine.calendar import session_bucket, trading_day

TRADING_DAYS_PER_YEAR = 252
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def max_drawdown(equity: list[float]) -> tuple[float, float]:
    """(max drawdown in currency, as % of the running peak), both >= 0."""
    peak = -math.inf
    dd_abs = dd_pct = 0.0
    for v in equity:
        peak = max(peak, v)
        d = peak - v
        dd_abs = max(dd_abs, d)
        if peak > 0 and d / peak > dd_pct:
            dd_pct = d / peak
    return dd_abs, dd_pct * 100


def daily_equity(equity: pl.DataFrame, initial: float) -> pl.DataFrame:
    """Equity at the end of each weekday trading day (forward-filled over quiet days)."""
    if equity.height == 0:
        return pl.DataFrame(schema={"day": pl.Int32, "equity": pl.Float64})
    d = (
        equity.sort("ts")
        .group_by(day=trading_day(pl.col("ts")), maintain_order=True)
        .agg(pl.col("equity").last())
        .sort("day")
    )
    all_days = pl.DataFrame({"day": pl.int_range(d["day"].min(), d["day"].max() + 1, eager=True)
                             .cast(pl.Int32)})
    out = all_days.join(d, on="day", how="left").with_columns(pl.col("equity").forward_fill())
    weekday = (pl.col("day").cast(pl.Date).dt.weekday())  # Mon=1..Sun=7
    return out.filter(weekday <= 5).with_columns(pl.col("equity").fill_null(initial))


def sharpe(daily: pl.DataFrame) -> float | None:
    rets = daily["equity"].pct_change().drop_nulls()
    if rets.len() < 2:
        return None
    sd = rets.std()
    if not sd:
        return None
    return float(rets.mean() / sd * math.sqrt(TRADING_DAYS_PER_YEAR))


def compute_metrics(result, sessions: dict) -> dict:
    t = result.trades
    init = result.initial_balance
    n = t.height
    eq_values = [init] + result.equity["equity"].to_list()
    dd_abs, dd_pct = max_drawdown(eq_values)
    daily = daily_equity(result.equity, init)
    net = float(t["net_pnl"].sum()) if n else 0.0
    wins = t.filter(pl.col("net_pnl") > 0)
    losses = t.filter(pl.col("net_pnl") <= 0)
    gp = float(wins["net_pnl"].sum()) if wins.height else 0.0
    gl = float(losses["net_pnl"].sum()) if losses.height else 0.0
    m = {
        "trades": n,
        "net_profit": round(net, 2),
        "return_pct": round(net / init * 100, 3),
        "gross_profit": round(gp, 2),
        "gross_loss": round(gl, 2),
        "profit_factor": round(gp / -gl, 3) if gl < 0 else None,
        "win_rate_pct": round(wins.height / n * 100, 2) if n else None,
        "avg_trade": round(net / n, 2) if n else None,
        "avg_win": round(gp / wins.height, 2) if wins.height else None,
        "avg_loss": round(gl / losses.height, 2) if losses.height else None,
        "avg_r": round(float(t["r_multiple"].mean()), 3) if n else None,
        "max_drawdown": round(dd_abs, 2),
        "max_drawdown_pct": round(dd_pct, 3),
        "sharpe": _round(sharpe(daily), 3),
        "long_trades": t.filter(pl.col("side") == "long").height,
        "short_trades": t.filter(pl.col("side") == "short").height,
        "costs": {
            "commission": round(float(t["commission"].sum()), 2) if n else 0.0,
            "spread": round(float(t["spread_cost"].sum()), 2) if n else 0.0,
            "slippage": round(float(t["slippage_cost"].sum()), 2) if n else 0.0,
            "swap": round(float(t["swap"].sum()), 2) if n else 0.0,
        },
        "exit_reasons": dict(t.group_by("exit_reason").len().iter_rows()) if n else {},
        "engine": {k: v for k, v in result.stats.items() if k != "final_balance"},
    }
    return m


def breakdowns(result, sessions: dict) -> dict[str, pl.DataFrame]:
    """Net result per month, weekday and session (by entry time)."""
    return breakdowns_from_trades(result.trades, sessions)


def breakdowns_from_trades(t: pl.DataFrame, sessions: dict) -> dict[str, pl.DataFrame]:
    if t.height == 0:
        return {}
    t = t.with_columns(
        month=pl.col("entry_ts").dt.strftime("%Y-%m"),
        weekday=pl.col("entry_ts").dt.weekday(),
        session=session_bucket(t, "entry_ts", sessions),
    )

    def agg(by: str) -> pl.DataFrame:
        return (
            t.group_by(by)
            .agg(
                trades=pl.len(),
                net=pl.col("net_pnl").sum().round(2),
                win_rate_pct=((pl.col("net_pnl") > 0).mean() * 100).round(1),
                avg=pl.col("net_pnl").mean().round(2),
            )
            .sort(by)
        )

    wd = agg("weekday").with_columns(
        pl.col("weekday").map_elements(lambda d: WEEKDAYS[d - 1], return_dtype=pl.Utf8))
    return {"month": agg("month"), "weekday": wd, "session": agg("session")}


def _round(v, n):
    return None if v is None else round(v, n)
