"""Engine behaviour: stops, targets, gaps, pending orders, OCO, expiry, daily loss limit."""

from datetime import UTC, datetime, timedelta

import pytest

from ctlab.engine.backtest import EngineConfig
from tests.engine_helpers import T0, flat, run

RISK3 = EngineConfig(100_000, 3.0, 2.0, 20)   # 3000 / 1009 per lot -> 2.97 lots, 2% daily limit


def test_sl_and_tp_in_same_bar_counts_as_sl():
    rows = flat(2) + [(2400, 2415, 2385, 2400)] + flat(2)
    res, _ = run({0: lambda ctx: ctx.buy(10.0, 10.0)}, rows)
    t = res.trades.row(0, named=True)
    assert t["exit_reason"] == "sl"
    assert t["exit_price"] == pytest.approx(2400.22 - 10 - 0.02)


def test_gap_through_stop_fills_at_open():
    rows = flat(2) + [(2380, 2381, 2379, 2380)] + flat(2, 2380)
    res, _ = run({0: lambda ctx: ctx.buy(10.0)}, rows)
    t = res.trades.row(0, named=True)
    assert t["exit_reason"] == "sl"
    assert t["exit_price"] == pytest.approx(2379.98)    # open 2380 - slip, not the SL 2390.22


def test_short_take_profit_needs_the_ask_to_reach_it():
    rows = flat(2) + [(2396, 2396, 2394.90, 2395), (2395, 2395, 2394.70, 2395)] + flat(2, 2395)
    res, _ = run({0: lambda ctx: ctx.sell(10.0, 5.0)}, rows)
    t = res.trades.row(0, named=True)
    # entry 2399.98 -> TP 2394.98. Bar 2: ask low 2395.10 (no). Bar 3: ask low 2394.90 (yes)
    assert t["exit_reason"] == "tp"
    assert t["exit_ts"] == T0 + timedelta(minutes=3)
    assert t["exit_price"] == pytest.approx(2394.98)


def test_short_stop_triggers_on_ask():
    # entry 2399.98, SL 2409.98 on the ask. Bid high 2409.85 -> ask 2410.05 >= SL
    rows = flat(2) + [(2400, 2409.85, 2399, 2400)] + flat(2)
    res, _ = run({0: lambda ctx: ctx.sell(10.0)}, rows)
    t = res.trades.row(0, named=True)
    assert t["exit_reason"] == "sl"
    assert t["exit_price"] == pytest.approx(2409.98 + 0.02)


def test_no_take_profit_on_the_entry_bar_of_a_stop_order():
    rows = [(2400, 2400, 2400, 2400),
            (2400, 2410, 2399, 2405),     # stop 2402 fills; TP 2403.02 touched in same bar: ignored
            (2402, 2404, 2401, 2403)] + flat(2, 2403)
    res, _ = run({0: lambda ctx: ctx.buy_stop(2402.0, 10.0, 1.0)}, rows)
    t = res.trades.row(0, named=True)
    assert t["entry_ts"] == T0 + timedelta(minutes=1)
    assert t["exit_reason"] == "tp"
    assert t["exit_ts"] == T0 + timedelta(minutes=2)


def test_stop_loss_is_checked_on_the_entry_bar():
    rows = [(2400, 2400, 2400, 2400), (2400, 2403, 2380, 2390)] + flat(2, 2390)
    res, _ = run({0: lambda ctx: ctx.buy_stop(2402.0, 10.0, 50.0)}, rows)
    t = res.trades.row(0, named=True)
    assert t["exit_reason"] == "sl" and t["exit_ts"] == T0 + timedelta(minutes=1)
    assert t["exit_price"] == pytest.approx(2402.02 - 10 - 0.02)


def test_oco_cancels_the_other_order():
    def place(ctx):
        ctx.buy_stop(2405.0, 20.0)
        ctx.sell_stop(2395.0, 20.0)
    rows = [(2400, 2400, 2400, 2400), (2400, 2406, 2399, 2405), (2405, 2405, 2390, 2392)] + flat(3, 2392)
    res, _ = run({0: place, 3: lambda ctx: ctx.close()}, rows)
    assert res.trades.height == 1
    assert res.trades["side"][0] == "long"


def test_pending_order_expires():
    rows = flat(3) + [(2400, 2410, 2400, 2405)] + flat(2, 2405)
    res, _ = run({0: lambda ctx: ctx.buy_stop(2405.0, 10.0, expire_bars=2)}, rows)
    assert res.trades.height == 0
    assert res.stats["expired_orders"] == 1


def test_one_position_at_a_time():
    res, _ = run({0: lambda ctx: ctx.buy(10.0), 2: lambda ctx: ctx.buy(10.0)}, flat(6))
    assert res.trades.height == 1
    assert res.stats["ignored_in_position"] == 1


def test_close_and_reverse_on_same_bar():
    def flip(ctx):
        ctx.close()
        ctx.sell(10.0)
    res, _ = run({0: lambda ctx: ctx.buy(10.0), 2: flip}, flat(6))
    assert res.trades["side"].to_list() == ["long", "short"]
    assert res.trades["exit_ts"][0] == res.trades["entry_ts"][1]


def test_open_position_is_closed_at_end_of_data():
    res, _ = run({0: lambda ctx: ctx.buy(10.0)}, flat(3) + [(2401, 2401, 2401, 2401.5)])
    t = res.trades.row(0, named=True)
    assert t["exit_reason"] == "end_of_data"
    assert t["exit_price"] == pytest.approx(2401.5)


def test_daily_loss_limit_blocks_until_next_trading_day():
    # Tue 2024-01-09 21:50 UTC = 16:50 NY; new trading day at 22:00 UTC (17:00 NY)
    start = datetime(2024, 1, 9, 21, 50, tzinfo=UTC)
    rows = flat(2) + [(2400, 2400, 2385, 2385)] + flat(12, 2385)
    script = {0: lambda ctx: ctx.buy(10.0),     # loses ~3% -> limit (2%) hit
              4: lambda ctx: ctx.buy(10.0),     # same trading day: blocked
              10: lambda ctx: ctx.buy(10.0)}    # decided at 22:01 close -> new day: allowed
    res, strat = run(script, rows, cfg=RISK3, start=start)
    assert res.stats["daily_limit_hits"] == 1
    assert res.stats["blocked_daily_limit"] == 1
    assert res.trades.height == 2
    assert res.trades["entry_ts"][1] == datetime(2024, 1, 9, 22, 1, tzinfo=UTC)
    assert strat.seen[4]["halted"] is True


def test_daily_loss_limit_closes_open_position():
    rows = flat(2) + [(2400, 2400, 2393, 2393)] + flat(3, 2393)
    res, _ = run({0: lambda ctx: ctx.buy(10.0)}, rows, cfg=RISK3)
    t = res.trades.row(0, named=True)
    # unrealized (2393 - 2400.22) * 297 = -2144 < -2% of 100k -> close at next open
    assert t["exit_reason"] == "daily_limit"
    assert t["exit_ts"] == T0 + timedelta(minutes=3)
    assert t["exit_price"] == pytest.approx(2392.98)


def test_risk_per_trade_matches_loss_at_stop():
    rows = flat(2) + [(2400, 2400, 2380, 2380)] + flat(2, 2380)
    res, _ = run({0: lambda ctx: ctx.buy(10.0)}, rows, cfg=RISK3)
    t = res.trades.row(0, named=True)
    assert t["lots"] == 2.97
    # loss = (10 + 0.02) * 297 + 2 * 3.5 * 2.97 = 2975.94 + 20.79 <= 3% of 100k
    assert t["net_pnl"] == pytest.approx(-(10.02 * 297 + 7 * 2.97))
    assert -t["net_pnl"] <= 3000
