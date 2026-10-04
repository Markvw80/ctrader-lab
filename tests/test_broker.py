"""Open API decoding/paging/spec logic, using the SDK's real protobuf classes (no network)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOASymbol,
    ProtoOATickData,
    ProtoOATrendbar,
)

from ctlab.broker.decode import (
    bars_frame,
    decode_ticks,
    decode_trendbar,
    page_bars_backward,
    page_ticks_backward,
    ticks_frame,
    windows,
)
from ctlab.broker.spec import merge_profiles, spread_profile, symbol_overrides
from ctlab.config import env

MIN = 60_000


def test_decode_trendbar_prices_are_scaled_by_1e5():
    tb = ProtoOATrendbar(volume=321, low=240_012_000, deltaOpen=50_000, deltaHigh=150_000,
                         deltaClose=100_000, utcTimestampInMinutes=28_000_000)
    ts, o, h, lo, c, v = decode_trendbar(tb)
    assert ts == 28_000_000 * MIN
    assert (o, h, lo, c, v) == (2400.62, 2401.62, 2400.12, 2401.12, 321)


def test_decode_ticks_delta_encoding_newest_first():
    data = [ProtoOATickData(timestamp=1_000_000, tick=240_000_000),
            ProtoOATickData(timestamp=-5, tick=1_000),
            ProtoOATickData(timestamp=-3, tick=-2_000)]
    assert decode_ticks(data) == [(1_000_000, 2400.0), (999_995, 2400.01), (999_992, 2399.99)]


def test_windows_cover_range_without_overlap():
    w = windows(0, 10, 4)
    assert w == [(0, 4), (4, 8), (8, 10)]


class FakeBarServer:
    """Returns at most `cap` bars nearest to `to` (inclusive), like the real API."""

    def __init__(self, ts_list, cap):
        self.ts, self.cap, self.calls = sorted(ts_list), cap, 0

    async def fetch(self, f, t):
        self.calls += 1
        hits = [x for x in self.ts if f <= x <= t][-self.cap:]
        return [(x, 1.0, 1.0, 1.0, 1.0, 1) for x in hits]


def test_page_bars_backward_recovers_everything_despite_cap():
    # 3 days of minutes with a 2-day hole in the middle (weekend)
    ts = [i * MIN for i in range(1440)] + [i * MIN for i in range(4 * 1440, 5 * 1440)]
    srv = FakeBarServer(ts, cap=500)
    rows = asyncio.run(page_bars_backward(srv.fetch, 0, 5 * 1440 * MIN))
    df = bars_frame(rows, digits=2)
    assert df.height == len(ts)
    assert df["ts"].is_sorted() and df["ts"].n_unique() == df.height
    assert srv.calls < 20


def test_page_bars_stops_on_empty_window():
    srv = FakeBarServer([], cap=500)
    assert asyncio.run(page_bars_backward(srv.fetch, 0, 1440 * MIN)) == []
    assert srv.calls == 1


def test_page_ticks_follows_has_more():
    all_ticks = [(1000 + i, 2400.0 + i / 100) for i in range(2500)]

    async def fetch(f, t):
        hits = [x for x in all_ticks if f <= x[0] <= t][::-1]   # newest first
        return hits[:1000], len(hits) > 1000

    ticks = asyncio.run(page_ticks_backward(fetch, 0, 10_000))
    assert {t[0] for t in ticks} == {t[0] for t in all_ticks}


def test_ticks_frame_merges_bid_ask_with_forward_fill():
    bid = [(1000, 2400.00), (3000, 2400.10)]
    ask = [(2000, 2400.20), (4000, 2400.30)]
    q = ticks_frame(bid, ask, digits=2)
    # first quote only once both sides are known
    assert q["ts"].dt.epoch("ms").to_list() == [2000, 3000, 4000]
    assert q["bid"].to_list() == [2400.00, 2400.10, 2400.10]
    assert q["ask"].to_list() == [2400.20, 2400.20, 2400.30]


def test_symbol_overrides_from_proto():
    sym = ProtoOASymbol(symbolId=41, digits=2, pipPosition=1, lotSize=10_000, minVolume=100,
                        stepVolume=100, commissionType=1, preciseTradingCommissionRate=3_000_000_000,
                        swapCalculationType=0, swapLong=-41.5, swapShort=12.3, swapRollover3Days=3)
    o = symbol_overrides(sym)
    assert o["contract_size"] == 100
    assert o["min_lot"] == 0.01 and o["lot_step"] == 0.01
    assert o["tick_size"] == pytest.approx(0.01) and o["pip_size"] == pytest.approx(0.1)
    assert o["costs"]["commission"] == {"type": "usd_per_million_notional", "value_per_side": 30.0}
    assert o["costs"]["swap"]["long"] == -41.5 and o["costs"]["swap"]["triple_day"] == "wednesday"


def test_spread_profile_uses_new_york_hours():
    # 2024-01-09 is winter (NY = UTC-5): 15:00 UTC = 10:00 NY, 21:30 UTC = 16:30 NY
    t0 = datetime(2024, 1, 9, 15, 0, tzinfo=UTC)
    t1 = datetime(2024, 1, 9, 21, 30, tzinfo=UTC)
    rows = [(t0 + timedelta(seconds=10 * i), 2400.0, 2400.2) for i in range(60)]
    rows += [(t1 + timedelta(seconds=10 * i), 2400.0, 2400.9) for i in range(60)]
    q = pl.DataFrame(rows, schema=["ts", "bid", "ask"], orient="row")
    p = spread_profile(q)
    assert p["hours"][10]["median"] == pytest.approx(0.2)
    assert p["hours"][16]["median"] == pytest.approx(0.9)
    merged = merge_profiles([p, p])
    assert merged["hours"][10]["minutes"] == 2 * p["hours"][10]["minutes"]


def test_broker_overrides_win_over_yaml(tmp_path, monkeypatch):
    from ctlab.config import symbol_spec

    monkeypatch.setenv("CTLAB_DATA_DIR", str(tmp_path))
    env.cache_clear()
    try:
        (tmp_path / "broker").mkdir()
        (tmp_path / "broker" / "XAUUSD.json").write_text(
            json.dumps({"costs": {"commission": {"value_per_side": 35.0}}}))
        spec = symbol_spec("XAUUSD")
        assert spec["costs"]["commission"]["value_per_side"] == 35.0
        assert spec["costs"]["commission"]["type"] == "usd_per_million_notional"  # kept from yaml
        assert spec["contract_size"] == 100
    finally:
        env.cache_clear()


def test_saved_tokens_win_over_env(tmp_path, monkeypatch):
    from ctlab.config import save_tokens, tokens

    monkeypatch.setenv("CTLAB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CTRADER_ACCESS_TOKEN", "from-env")
    env.cache_clear()
    try:
        assert tokens()[0] == "from-env"
        p = save_tokens("new-access", "new-refresh", 86400)
        assert tokens() == ("new-access", "new-refresh")
        assert oct(p.stat().st_mode & 0o777) == "0o600"
    finally:
        env.cache_clear()
