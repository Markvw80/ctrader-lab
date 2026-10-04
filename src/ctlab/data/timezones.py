"""Timezone handling for imported data.

Besides IANA names ("UTC", "Europe/London") we accept the MetaTrader-style broker
server clock "NY+7": UTC+2 in winter / UTC+3 in summer, i.e. New York time plus 7 hours,
so that the daily rollover (17:00 New York) is always 00:00 server time.
"""

import re

import polars as pl

NY = "America/New_York"
_NY_OFFSET = re.compile(r"^NY([+-]\d{1,2})$", re.IGNORECASE)


def localize_to_utc(ts: pl.Expr, tz: str) -> pl.Expr:
    """Interpret naive timestamps as wall-clock time in `tz` and convert to UTC.

    Non-existent local times (DST spring-forward) become null and are reported by validation.
    """
    m = _NY_OFFSET.match(tz)
    if m:
        ts = ts - pl.duration(hours=int(m.group(1)))
        tz = NY
    return ts.dt.replace_time_zone(tz, ambiguous="earliest", non_existent="null").dt.convert_time_zone(
        "UTC"
    )
