from datetime import UTC, datetime

from ctlab.data.csv_import import clean, load_format, read_csv


def test_mt5_format(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text(
        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
        "2024.07.15\t10:00:00\t2400.10\t2401.00\t2399.50\t2400.80\t120\t0\t15\n"
        "2024.07.15\t10:01:00\t2400.80\t2402.00\t2400.00\t2401.50\t95\t0\t14\n"
    )
    df = read_csv(p, load_format("mt5"))
    assert df.columns == ["ts", "open", "high", "low", "close", "tick_volume"]
    assert df["ts"][0] == datetime(2024, 7, 15, 7, 0, tzinfo=UTC)
    assert df["tick_volume"].to_list() == [120, 95]
    assert df["close"][1] == 2401.50


def test_dukascopy_without_volume_mapping(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(
        "Gmt time,Open,High,Low,Close,Volume\n"
        "15.01.2024 10:00:00.000,2050.1,2051.0,2049.9,2050.5,0.12\n"
    )
    df = read_csv(p, load_format("dukascopy"))
    assert df["ts"][0] == datetime(2024, 1, 15, 10, 0, tzinfo=UTC)
    assert df["tick_volume"][0] == 0


def test_ticks_format(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("Gmt time,Ask,Bid,AskVolume,BidVolume\n15.01.2024 10:00:00.123,2050.30,2050.10,1,1\n")
    df = read_csv(p, load_format("dukascopy_ticks"))
    assert df.columns == ["ts", "bid", "ask"]
    assert df["ts"][0] == datetime(2024, 1, 15, 10, 0, 0, 123000, tzinfo=UTC)


def test_clean_drops_exact_duplicates_and_sorts(tmp_path):
    p = tmp_path / "g.csv"
    p.write_text(
        "ts,open,high,low,close,volume\n"
        "2024-01-15 10:01:00,2,3,1,2,5\n"
        "2024-01-15 10:00:00,1,2,0.5,1.5,5\n"
        "2024-01-15 10:01:00,2,3,1,2,5\n"
    )
    df, stats = clean(read_csv(p, load_format("generic")))
    assert stats["exact_duplicates_dropped"] == 1
    assert df["ts"].is_sorted() and df.height == 2
