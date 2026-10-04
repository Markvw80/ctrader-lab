from datetime import UTC, datetime

import polars as pl

from ctlab.data import store
from tests.conftest import make_m1


def test_roundtrip_partitions_and_merge(tmp_path):
    a = make_m1(datetime(2023, 12, 27, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC), seed=1)
    store.write(a, "XAUUSD", "M1", root=tmp_path)
    assert sorted(p.name for p in (tmp_path / "XAUUSD" / "M1").glob("*.parquet")) == [
        "2023.parquet", "2024.parquet"]

    # overlapping update: new values win, no duplicates
    upd = a.tail(10).with_columns(close=pl.col("close") + 1, high=pl.col("high") + 1)
    store.write(upd, "XAUUSD", "M1", root=tmp_path)
    back = store.read("XAUUSD", "M1", root=tmp_path)
    assert back.height == a.height
    assert back["ts"].is_sorted() and back["ts"].n_unique() == back.height
    assert back.tail(10)["close"].to_list() == upd["close"].to_list()


def test_read_range_is_half_open(tmp_path):
    a = make_m1(datetime(2024, 1, 8, tzinfo=UTC), datetime(2024, 1, 9, tzinfo=UTC))
    store.write(a, "XAUUSD", "M1", root=tmp_path)
    # naive datetimes are interpreted as UTC
    s, e = datetime(2024, 1, 8, 10, 0), datetime(2024, 1, 8, 11, 0)  # noqa: DTZ001
    df = store.read("XAUUSD", "M1", s, e, root=tmp_path)
    assert df.height == 60
    assert df["ts"].min() == s.replace(tzinfo=UTC)
    assert df["ts"].max() == datetime(2024, 1, 8, 10, 59, tzinfo=UTC)
