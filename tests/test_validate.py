from datetime import UTC, datetime, timedelta

import polars as pl

from ctlab.data.validate import validate_bars, validate_ticks
from tests.conftest import make_m1

WEEK = (datetime(2024, 1, 7, tzinfo=UTC), datetime(2024, 1, 13, tzinfo=UTC))


def codes(rep):
    return {i.code for i in rep.issues}


def test_clean_week_has_no_issues(hours):
    rep = validate_bars(make_m1(*WEEK), "M1", hours)
    assert rep.ok, rep.issues
    assert rep.gaps["missing_bars"] == 0
    assert "timezone_suspect" not in codes(rep)


def test_detects_large_gap(hours):
    df = make_m1(*WEEK)
    hole_start = datetime(2024, 1, 9, 14, 0, tzinfo=UTC)
    df = df.filter(~pl.col("ts").is_between(hole_start, hole_start + timedelta(minutes=89)))
    rep = validate_bars(df, "M1", hours)
    assert rep.ok  # gaps are warnings, not errors
    assert "large_gaps" in codes(rep)
    assert rep.gaps["missing_bars"] == 90
    assert rep.gaps["largest"][0]["minutes"] == 90


def test_daily_break_and_weekend_are_not_gaps(hours):
    rep = validate_bars(make_m1(*WEEK), "M1", hours)
    assert rep.gaps["gaps"] == 0


def test_conflicting_duplicates_are_errors(hours):
    df = make_m1(*WEEK)
    dup = df.head(1).with_columns(close=pl.col("close") + 5, high=pl.col("high") + 5)
    rep = validate_bars(pl.concat([df, dup]).sort("ts"), "M1", hours)
    assert not rep.ok and "conflicting_duplicates" in codes(rep)


def test_bad_ohlc_is_error(hours):
    df = make_m1(*WEEK).with_row_index().with_columns(
        high=pl.when(pl.col("index") == 100).then(pl.col("low") - 1).otherwise(pl.col("high"))
    ).drop("index")
    rep = validate_bars(df, "M1", hours)
    assert "ohlc_inconsistent" in codes(rep)


def test_misaligned_timestamps(hours):
    df = make_m1(*WEEK).with_columns(pl.col("ts") + timedelta(seconds=30))
    assert "misaligned" in codes(validate_bars(df, "M1", hours))


def test_timezone_shift_detected(hours):
    df = make_m1(*WEEK).with_columns(pl.col("ts") + timedelta(hours=2))
    rep = validate_bars(df, "M1", hours)
    tz = next(i for i in rep.issues if i.code == "timezone_suspect")
    assert "+2" in tz.message or "2]" in tz.message
    assert not rep.ok


def test_dst_error_detected_only_in_affected_month(hours):
    jan = make_m1(datetime(2024, 1, 7, tzinfo=UTC), datetime(2024, 1, 13, tzinfo=UTC))
    jul = make_m1(datetime(2024, 7, 7, tzinfo=UTC), datetime(2024, 7, 13, tzinfo=UTC))
    # Source exported with a fixed winter offset: summer bars end up 1h late
    jul = jul.with_columns(pl.col("ts") + timedelta(hours=1))
    rep = validate_bars(pl.concat([jan, jul]), "M1", hours)
    tz = next(i for i in rep.issues if i.code == "timezone_suspect")
    assert tz.count == 1 and tz.samples[0].startswith("2024-07")


def test_crossed_ticks(hours):
    ts = [datetime(2024, 1, 9, 10, 0, s, tzinfo=UTC) for s in range(3)]
    df = pl.DataFrame({"ts": ts, "bid": [1.0, 2.0, 3.0], "ask": [1.1, 1.9, 3.1]})
    assert "crossed_quotes" in codes(validate_ticks(df, hours))
