"""Single entry point for strategies/engine to get bars."""

from datetime import datetime

import polars as pl

from ctlab.data import store
from ctlab.data.resample import resample

BASE_TF = "M1"


def load_bars(symbol: str, timeframe: str, start: datetime | None = None,
              end: datetime | None = None) -> pl.DataFrame:
    """Stored bars if that timeframe exists on disk, otherwise resampled from M1."""
    tf = timeframe.upper()
    if store.dataset_dir(symbol, tf).exists() and any(store.dataset_dir(symbol, tf).glob("*.parquet")):
        return store.read(symbol, tf, start, end)
    return resample(store.read(symbol, BASE_TF, start, end), tf)
