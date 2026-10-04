"""Reference strategy: Asia-range breakout at the London open.

Purpose: demonstrate the framework (pending stop orders, OCO, session logic, flatten time).
Not a claim of profitability.

- Range = high/low of the Asia session within the current trading day.
- At the first bar of the London session: buy stop above the range, sell stop below (OCO).
- SL = range width * sl_mult, TP = SL * rr. Pending orders expire after entry_window_bars.
- Everything is closed at flatten_hour_ny (New York time).
"""

import numpy as np
import polars as pl

from ctlab.strategy.base import Context, Strategy, register
from ctlab.strategy.params import FloatParam, IntParam


@register
class SessionBreakout(Strategy):
    name = "session_breakout"
    timeframe = "M15"

    buffer = FloatParam(0.5, 0.0, 3.0, step=0.1, doc="distance beyond range for the stop orders (USD)")
    sl_mult = FloatParam(1.0, 0.3, 2.0, step=0.1, doc="stop loss as multiple of range width")
    rr = FloatParam(1.5, 0.5, 4.0, step=0.1, doc="take profit as multiple of stop loss")
    min_range = FloatParam(3.0, 0.5, 15.0, step=0.5, doc="skip days with a narrower Asia range (USD)")
    max_range = FloatParam(30.0, 10.0, 80.0, step=1.0, doc="skip days with a wider Asia range (USD)")
    entry_window_bars = IntParam(16, 4, 32, doc="pending order lifetime in bars")
    flatten_hour_ny = IntParam(16, 11, 16, doc="close everything at this New York hour")

    def prepare(self, bars: pl.DataFrame) -> dict[str, np.ndarray]:
        asia = pl.col("sess_asia")
        df = bars.with_columns(
            pl.when(asia).then(pl.col("high")).otherwise(None).alias("_h"),
            pl.when(asia).then(pl.col("low")).otherwise(None).alias("_l"),
        ).with_columns(
            pl.col("_h").cum_max().forward_fill().over("trading_day").alias("range_high"),
            pl.col("_l").cum_min().forward_fill().over("trading_day").alias("range_low"),
            (pl.col("sess_london") & ~pl.col("sess_london").shift(1).fill_null(False))
            .alias("london_open"),
        )
        return {
            "range_high": df["range_high"].fill_null(np.nan).to_numpy(),
            "range_low": df["range_low"].fill_null(np.nan).to_numpy(),
            "london_open": df["london_open"].to_numpy(),
        }

    def on_bar(self, ctx: Context) -> None:
        bar_close_minute = ctx.now("ny_minute") + 15
        if bar_close_minute >= self.flatten_hour_ny * 60:
            if ctx.position is not None:
                ctx.close()
            if ctx.has_pending:
                ctx.cancel_all()
            return
        if not ctx.now("london_open") or ctx.position is not None:
            return
        hi, lo = ctx.now("range_high"), ctx.now("range_low")
        if np.isnan(hi) or np.isnan(lo):
            return
        width = hi - lo
        if not (self.min_range <= width <= self.max_range):
            return
        sl = width * self.sl_mult
        tp = sl * self.rr
        ctx.buy_stop(hi + self.buffer, sl, tp, expire_bars=self.entry_window_bars, tag="breakout_up")
        ctx.sell_stop(lo - self.buffer, sl, tp, expire_bars=self.entry_window_bars, tag="breakout_down")
