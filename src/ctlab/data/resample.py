"""Resample M1 bars to higher timeframes.

Bars >= H1 are aligned to the broker server clock (New York + 7h), so that D1 starts at the
17:00 New York rollover and H4 matches cTrader/MT charts. The DST switch hours fall on
Sunday early morning New York time, when the market is closed, so the repeated local hour
never contains data.
"""

import polars as pl

from ctlab.data.schema import BAR_SCHEMA, TIMEFRAMES, timeframe_minutes
from ctlab.data.timezones import NY

SERVER_OFFSET_H = 7

_AGG = [
    pl.col("open").first(),
    pl.col("high").max(),
    pl.col("low").min(),
    pl.col("close").last(),
    pl.col("tick_volume").sum(),
]


def resample(df: pl.DataFrame, timeframe: str) -> pl.DataFrame:
    tf = timeframe.upper()
    every = TIMEFRAMES[tf]
    if timeframe_minutes(tf) < 60:
        out = df.sort("ts").group_by_dynamic("ts", every=every, closed="left", label="left").agg(_AGG)
        return out.select(list(BAR_SCHEMA))
    srv = (
        pl.col("ts").dt.convert_time_zone(NY).dt.replace_time_zone(None)
        + pl.duration(hours=SERVER_OFFSET_H)
    )
    out = (
        df.sort("ts")
        .with_columns(_srv=srv)
        .group_by_dynamic("_srv", every=every, closed="left", label="left")
        .agg(_AGG)
        .with_columns(
            ts=(pl.col("_srv") - pl.duration(hours=SERVER_OFFSET_H))
            .dt.replace_time_zone(NY, ambiguous="earliest", non_existent="null")
            .dt.convert_time_zone("UTC")
        )
    )
    return out.select(list(BAR_SCHEMA))
