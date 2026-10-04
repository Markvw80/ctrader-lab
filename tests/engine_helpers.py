"""Hand-built bars, a scripted strategy and a simple cost model for engine tests."""

from datetime import UTC, datetime, timedelta

import polars as pl

from ctlab.engine.backtest import EngineConfig, run_backtest
from ctlab.engine.costs import CostModel
from ctlab.strategy.base import Strategy

# Tuesday 2024-01-09 10:00 UTC = 05:00 New York: far from the 17:00 NY rollover
T0 = datetime(2024, 1, 9, 10, 0, tzinfo=UTC)


def bars(rows, start: datetime = T0, step_min: int = 1) -> pl.DataFrame:
    """rows: (open, high, low, close) per bar."""
    return pl.DataFrame({
        "ts": [start + timedelta(minutes=step_min * i) for i in range(len(rows))],
        "open": [float(r[0]) for r in rows],
        "high": [float(r[1]) for r in rows],
        "low": [float(r[2]) for r in rows],
        "close": [float(r[3]) for r in rows],
        "tick_volume": [1] * len(rows),
    }).with_columns(pl.col("ts").dt.cast_time_unit("us"))


def flat(n: int, price: float = 2400.0):
    return [(price, price, price, price)] * n


def cost_model(**kw) -> CostModel:
    base = {"contract_size": 100.0, "tick_size": 0.01, "pip_size": 0.1, "min_lot": 0.01,
            "lot_step": 0.01, "commission_type": "usd_per_lot", "commission_value": 3.5,
            "slippage_ticks": 2, "swap_calculation": "usd_per_lot", "swap_long": 0.0,
            "swap_short": 0.0, "triple_day": 2, "spread_hours": {}, "spread_default": 0.2}
    base.update(kw)
    return CostModel(**base)


# 1.009% of 100k = 1009 = loss of exactly 1.00 lot at a 10.00 stop (+0.02 slippage, 2x3.5 commission)
ONE_LOT = EngineConfig(initial_balance=100_000, risk_per_trade_pct=1.009,
                       daily_loss_limit_pct=None, max_leverage=20)


class Scripted(Strategy):
    """Runs script[i](ctx) at the close of bar i and records what it could see."""

    name = "_scripted"
    timeframe = "M1"

    def __init__(self, script: dict, timeframe: str = "M1"):
        super().__init__()
        self.script = script
        self.timeframe = timeframe
        self.seen: list[dict] = []

    def on_bar(self, ctx):
        self.seen.append({"i": ctx.i, "n_visible": len(ctx.bars.close),
                          "last_close": float(ctx.bars.close[-1]), "last_ts": int(ctx.bars.ts[-1]),
                          "position": ctx.position, "halted": ctx.halted})
        f = self.script.get(ctx.i)
        if f:
            f(ctx)


def run(script, rows, cost=None, cfg=ONE_LOT, timeframe="M1", start=T0, sessions=None):
    strat = Scripted(script, timeframe)
    res = run_backtest(strat, bars(rows, start), cost or cost_model(), sessions or {}, cfg)
    return res, strat
