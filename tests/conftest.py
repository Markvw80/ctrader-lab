from datetime import datetime

import numpy as np
import polars as pl
import pytest

from ctlab.data.market_hours import MarketHours


def make_m1(start: datetime, end: datetime, seed: int = 0) -> pl.DataFrame:
    """Synthetic M1 bars for every minute the market is open in [start, end)."""
    grid = pl.datetime_range(start, end, "1m", eager=True, time_zone="UTC", closed="left")
    df = pl.DataFrame({"ts": grid}).filter(~MarketHours().closed(pl.col("ts")))
    n = df.height
    rng = np.random.default_rng(seed)
    close = 2000 + np.cumsum(rng.normal(0, 0.3, n))
    open_ = np.r_[close[0], close[:-1]]
    return df.with_columns(
        open=pl.Series(open_),
        high=pl.Series(np.maximum(open_, close) + rng.uniform(0, 0.2, n)),
        low=pl.Series(np.minimum(open_, close) - rng.uniform(0, 0.2, n)),
        close=pl.Series(close),
        tick_volume=pl.Series(rng.integers(1, 100, n)),
    )


@pytest.fixture
def hours() -> MarketHours:
    return MarketHours()
