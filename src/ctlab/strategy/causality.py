"""Automatic look-ahead checks for any strategy.

1. prepare(): indicators computed on a truncated history must equal those on the full history.
2. Engine: perturbing all prices after a cut time must not change
   - any decision (orders / close / cancel) taken on a bar that closed before the cut, and
   - any trade that ended before the cut.
"""

from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import polars as pl

from ctlab.data.resample import resample
from ctlab.data.schema import timeframe_minutes
from ctlab.engine.backtest import EngineConfig, run_backtest
from ctlab.engine.calendar import add_calendar
from ctlab.engine.costs import CostModel
from ctlab.strategy.base import Strategy


@dataclass
class CausalityReport:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    checks: int = 0


def check_prepare(strategy: Strategy, strat_bars: pl.DataFrame, cuts=(0.25, 0.5, 0.75, 0.9)) -> CausalityReport:
    rep = CausalityReport()
    full = strategy.prepare(strat_bars)
    n = strat_bars.height
    for frac in cuts:
        cut = max(2, int(n * frac))
        part = strategy.prepare(strat_bars.head(cut))
        for name, arr in full.items():
            a = np.asarray(arr)[:cut]
            b = np.asarray(part[name])
            rep.checks += 1
            same = np.array_equal(a, b) if a.dtype == bool or b.dtype == bool else \
                np.allclose(a.astype(float), b.astype(float), equal_nan=True, rtol=1e-9, atol=1e-12)
            if not same:
                bad = int(np.argmax(~np.isclose(a.astype(float), b.astype(float), equal_nan=True)))
                rep.ok = False
                rep.problems.append(f"prepare(): '{name}' at row {bad} changes when future rows are "
                                    f"removed (cut at {cut}/{n}) -> uses future data")
    return rep


def check_engine(strategy_cls: type[Strategy], params: dict, exec_bars: pl.DataFrame, cost: CostModel,
                 sessions: dict, cfg: EngineConfig | None = None, seed: int = 7,
                 cut_fracs=(0.4, 0.7)) -> CausalityReport:
    rep = CausalityReport()
    base, base_dec = _run_recording(strategy_cls, params, exec_bars, cost, sessions, cfg)
    rng = np.random.default_rng(seed)
    ts = exec_bars["ts"]
    for frac in cut_fracs:
        cut_ts = ts[int(len(ts) * frac)]
        after = pl.col("ts") >= cut_ts
        noise = pl.Series(rng.normal(1.0, 0.01, exec_bars.height))
        moved = exec_bars.with_columns(
            [pl.when(after).then(pl.col(c) * noise).otherwise(pl.col(c)).alias(c)
             for c in ("open", "close")]
        ).with_columns(
            pl.when(after).then(pl.max_horizontal("open", "close") * 1.002).otherwise(pl.col("high")).alias("high"),
            pl.when(after).then(pl.min_horizontal("open", "close") * 0.998).otherwise(pl.col("low")).alias("low"),
        )
        other, other_dec = _run_recording(strategy_cls, params, moved, cost, sessions, cfg)
        cut_ns = int(pl.Series([cut_ts]).dt.epoch("ns")[0])
        da = [d for d in base_dec if d[0] <= cut_ns]
        db = [d for d in other_dec if d[0] <= cut_ns]
        rep.checks += 1
        if da != db:
            first = next((x for x, y in zip(da, db, strict=False) if x != y), None)
            rep.ok = False
            rep.problems.append(
                f"engine: decisions on bars closed before {cut_ts} change when later prices change "
                f"(first difference at bar close {first[0] if first else '?'}) -> uses future data")
        # a trade may only depend on data before its exit bar ends
        safe = pl.col("exit_ts") + timedelta(minutes=1) <= cut_ts
        a, b = base.filter(safe), other.filter(safe)
        rep.checks += 1
        if not a.equals(b):
            rep.ok = False
            rep.problems.append(
                f"engine: trades closed before {cut_ts} differ after changing later prices "
                f"({a.height} vs {b.height} trades)")
    return rep


def _run_recording(strategy_cls, params, bars, cost, sessions, cfg):
    """Backtest while recording every decision as (bar_close_ns, orders, close, cancel)."""
    strat = strategy_cls(**params)
    tf_ns = timeframe_minutes(strat.timeframe) * 60_000_000_000
    decisions = []
    inner = strat.on_bar

    def recording(ctx):
        inner(ctx)
        orders = tuple((o.side, o.kind, round(o.sl_distance, 9),
                        None if o.tp_distance is None else round(o.tp_distance, 9),
                        None if o.price is None else round(o.price, 9), o.expire_bars)
                       for o in ctx.orders)
        decisions.append((int(ctx.bars.ts[-1]) + tf_ns, orders, ctx.close_requested,
                          ctx.cancel_requested))

    strat.on_bar = recording
    res = run_backtest(strat, bars, cost, sessions, cfg)
    return res.trades, decisions


def check_strategy(strategy_cls: type[Strategy], params: dict, exec_bars: pl.DataFrame,
                   cost: CostModel, sessions: dict) -> CausalityReport:
    strat = strategy_cls(**params)
    sb = exec_bars if strat.timeframe.upper() == "M1" else resample(exec_bars, strat.timeframe)
    r1 = check_prepare(strat, add_calendar(sb, sessions))
    r2 = check_engine(strategy_cls, params, exec_bars, cost, sessions)
    return CausalityReport(r1.ok and r2.ok, r1.problems + r2.problems, r1.checks + r2.checks)
