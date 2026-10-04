"""Canonical storage schemas. All timestamps are UTC; a bar's `ts` is its OPEN time."""

import polars as pl

TS_DTYPE = pl.Datetime("us", "UTC")

BAR_SCHEMA = {
    "ts": TS_DTYPE,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "tick_volume": pl.Int64,
}

TICK_SCHEMA = {
    "ts": TS_DTYPE,
    "bid": pl.Float64,
    "ask": pl.Float64,
}

TIMEFRAMES = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H4": "4h", "D1": "1d",
}


def timeframe_minutes(tf: str) -> int:
    tf = tf.upper()
    if tf not in TIMEFRAMES:
        raise ValueError(f"Unknown timeframe {tf!r}; expected one of {list(TIMEFRAMES)}")
    unit, n = tf[0], int(tf[1:])
    return n * {"M": 1, "H": 60, "D": 1440}[unit]


def conform(df: pl.DataFrame, schema: dict) -> pl.DataFrame:
    """Select and cast columns to the canonical schema (raises if a column is missing)."""
    missing = set(schema) - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")
    return df.select([pl.col(c).cast(t) for c, t in schema.items()])
