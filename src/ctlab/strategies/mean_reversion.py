"""Reference strategy: z-score mean reversion during the London and New York sessions.

Purpose: demonstrate the framework (market orders, indicator exits, warmup). Not a claim of
profitability.

- z = (close - SMA(lookback)) / StdDev(lookback); ATR for stops.
- Buy when z <= -entry_z, sell when z >= entry_z (only in London/NY session, flat).
- Exit when z crosses back through exit_z, or at SL/TP (ATR multiples), or at flatten time.
"""

import numpy as np
import polars as pl

from ctlab.strategy.base import LONG, Context, Strategy, register
from ctlab.strategy.params import FloatParam, IntParam


@register
class MeanReversion(Strategy):
    name = "mean_reversion"
    timeframe = "M15"

    lookback = IntParam(40, 10, 200, step=5, doc="bars for mean and standard deviation")
    entry_z = FloatParam(2.0, 1.0, 3.5, step=0.1)
    exit_z = FloatParam(0.0, -0.5, 1.0, step=0.1)
    atr_period = IntParam(14, 5, 50)
    sl_atr = FloatParam(1.5, 0.5, 4.0, step=0.1)
    tp_atr = FloatParam(2.0, 0.5, 6.0, step=0.1)
    flatten_hour_ny = IntParam(16, 11, 16)

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_bars = max(self.lookback, self.atr_period) + 1

    def prepare(self, bars: pl.DataFrame) -> dict[str, np.ndarray]:
        c = pl.col("close")
        prev_close = c.shift(1)
        tr = pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - prev_close).abs(),
            (pl.col("low") - prev_close).abs(),
        )
        df = bars.select(
            ((c - c.rolling_mean(self.lookback)) / c.rolling_std(self.lookback)).alias("z"),
            tr.rolling_mean(self.atr_period).alias("atr"),
            (pl.col("sess_london") | pl.col("sess_newyork")).alias("tradable"),
        )
        return {
            "z": df["z"].fill_null(np.nan).fill_nan(np.nan).to_numpy(),
            "atr": df["atr"].fill_null(np.nan).to_numpy(),
            "tradable": df["tradable"].to_numpy(),
        }

    def on_bar(self, ctx: Context) -> None:
        z, atr = ctx.now("z"), ctx.now("atr")
        late = ctx.now("ny_minute") + 15 >= self.flatten_hour_ny * 60
        pos = ctx.position
        if pos is not None:
            if late or pos.side == LONG and z >= self.exit_z or pos.side != LONG and z <= -self.exit_z:
                ctx.close()
            return
        if late or not ctx.now("tradable") or np.isnan(z) or np.isnan(atr) or atr <= 0:
            return
        if z <= -self.entry_z:
            ctx.buy(atr * self.sl_atr, atr * self.tp_atr, tag="revert_up")
        elif z >= self.entry_z:
            ctx.sell(atr * self.sl_atr, atr * self.tp_atr, tag="revert_down")
