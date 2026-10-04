from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from ctlab.data.timezones import localize_to_utc


def _conv(s: str, tz: str) -> datetime:
    df = pl.DataFrame({"t": [datetime.fromisoformat(s)]})
    return df.select(localize_to_utc(pl.col("t"), tz))["t"][0]


@pytest.mark.parametrize("local,tz,expected", [
    ("2024-01-15 10:00", "UTC", "2024-01-15 10:00"),
    ("2024-01-15 10:00", "NY+7", "2024-01-15 08:00"),   # winter: server = UTC+2
    ("2024-07-15 10:00", "NY+7", "2024-07-15 07:00"),   # summer: server = UTC+3
    ("2024-03-12 10:00", "NY+7", "2024-03-12 07:00"),   # US already on DST, EU not yet
    ("2024-01-15 10:00", "Europe/London", "2024-01-15 10:00"),
    ("2024-07-15 10:00", "Europe/London", "2024-07-15 09:00"),
])
def test_localize(local, tz, expected):
    assert _conv(local, tz) == datetime.fromisoformat(expected).replace(tzinfo=UTC)


def test_server_midnight_is_ny_rollover():
    # 00:00 server time == 17:00 New York, all year round
    for day in ("2024-01-16", "2024-07-16"):
        utc = _conv(f"{day} 00:00", "NY+7")
        assert utc.astimezone(ZoneInfo("America/New_York")).hour == 17
