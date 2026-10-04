"""Clock facts per bar (no market data involved, so no look-ahead risk).

- trading_day: day number of the broker server date (New York + 7h); a new trading day starts
  at the 17:00 New York rollover, all year round.
- sessions: bar OPEN time within the local session window of config/symbols/<SYMBOL>.yaml.
"""

import polars as pl

from ctlab.data.timezones import NY

SERVER_OFFSET_H = 7


def trading_day(ts: pl.Expr) -> pl.Expr:
    server = ts.dt.convert_time_zone(NY).dt.replace_time_zone(None) + pl.duration(hours=SERVER_OFFSET_H)
    return server.dt.date().cast(pl.Int32)  # days since 1970-01-01


def local_minute(ts: pl.Expr, tz: str) -> pl.Expr:
    local = ts.dt.convert_time_zone(tz)
    return local.dt.hour().cast(pl.Int32) * 60 + local.dt.minute().cast(pl.Int32)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def session_flag(ts: pl.Expr, spec: dict) -> pl.Expr:
    m = local_minute(ts, spec["tz"])
    start, end = _minutes(spec["start"]), _minutes(spec["end"])
    return (m >= start) & (m < end) if start < end else (m >= start) | (m < end)


def add_calendar(bars: pl.DataFrame, sessions: dict) -> pl.DataFrame:
    """Add trading_day, ny_minute and sess_<name> columns."""
    cols = [
        trading_day(pl.col("ts")).alias("trading_day"),
        local_minute(pl.col("ts"), NY).alias("ny_minute"),
        pl.col("ts").dt.convert_time_zone(NY).dt.weekday().alias("ny_weekday"),
    ]
    cols += [session_flag(pl.col("ts"), s).alias(f"sess_{name}") for name, s in sessions.items()]
    return bars.with_columns(cols)


def session_bucket(df: pl.DataFrame, ts_col: str, sessions: dict) -> pl.Series:
    """Reporting bucket for a timestamp: overlap / london / newyork / asia / other."""
    flags = df.select(**{n: session_flag(pl.col(ts_col), s) for n, s in sessions.items()})
    london = flags["london"] if "london" in flags.columns else pl.Series([False] * df.height)
    ny = flags["newyork"] if "newyork" in flags.columns else pl.Series([False] * df.height)
    asia = flags["asia"] if "asia" in flags.columns else pl.Series([False] * df.height)
    return (
        pl.DataFrame({"l": london, "n": ny, "a": asia})
        .select(
            pl.when(pl.col("l") & pl.col("n")).then(pl.lit("london_ny_overlap"))
            .when(pl.col("l")).then(pl.lit("london"))
            .when(pl.col("n")).then(pl.lit("newyork"))
            .when(pl.col("a")).then(pl.lit("asia"))
            .otherwise(pl.lit("other"))
        )
        .to_series()
        .alias("session")
    )
