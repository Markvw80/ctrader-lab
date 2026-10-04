from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import polars as pl

from ctlab.data.resample import resample
from tests.conftest import make_m1


def test_m5_aggregation_uses_only_its_own_minutes():
    m1 = make_m1(datetime(2024, 1, 9, 10, tzinfo=UTC), datetime(2024, 1, 9, 11, tzinfo=UTC))
    m5 = resample(m1, "M5")
    assert m5.height == 12
    first = m1.head(5)
    row = m5.row(0, named=True)
    assert row["ts"] == datetime(2024, 1, 9, 10, 0, tzinfo=UTC)  # labelled by OPEN time
    assert row["open"] == first["open"][0]
    assert row["close"] == first["close"][4]
    assert row["high"] == first["high"].max()
    assert row["low"] == first["low"].min()
    assert row["tick_volume"] == first["tick_volume"].sum()



def test_d1_starts_at_ny_rollover_winter_and_summer():
    # first bar of each D1 = 18:00 NY (17:00-18:00 is the break, so first data is 18:00)
    for start, end in [(datetime(2024, 1, 8, tzinfo=UTC), datetime(2024, 1, 12, tzinfo=UTC)),
                       (datetime(2024, 7, 8, tzinfo=UTC), datetime(2024, 7, 12, tzinfo=UTC))]:
        d1 = resample(make_m1(start, end), "D1")
        full_days = d1.slice(1, d1.height - 2)  # edges are partial
        hours = {t.astimezone(ZoneInfo("America/New_York")).hour
                 for t in full_days["ts"]}
        assert hours == {17}, hours


def test_h1_close_equals_last_m1_close():
    m1 = make_m1(datetime(2024, 1, 9, tzinfo=UTC), datetime(2024, 1, 10, tzinfo=UTC))
    h1 = resample(m1, "H1")
    for row in h1.iter_rows(named=True):
        inside = m1.filter((pl.col("ts") >= row["ts"]) & (pl.col("ts") < row["ts"] + pl.duration(hours=1)))
        assert row["close"] == inside["close"][-1]
