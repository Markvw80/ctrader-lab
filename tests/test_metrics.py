from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from ctlab.engine.backtest import BacktestResult, _trades_frame
from ctlab.report.metrics import breakdowns, compute_metrics, max_drawdown

SESSIONS = {"asia": {"tz": "Asia/Tokyo", "start": "08:00", "end": "16:00"},
            "london": {"tz": "Europe/London", "start": "08:00", "end": "16:30"},
            "newyork": {"tz": "America/New_York", "start": "08:00", "end": "17:00"}}


def test_max_drawdown():
    dd, pct = max_drawdown([100, 120, 90, 130, 100])
    assert dd == 30 and pct == pytest.approx(25.0)


def _result(pnls, start=datetime(2024, 1, 8, 9, tzinfo=UTC)):
    ns = lambda d: int(d.timestamp() * 1e9)
    trades, eq_ts, eq, bal = [], [], [], 10_000.0
    for i, p in enumerate(pnls):
        entry = start + timedelta(days=i)
        bal += p
        trades.append({"signal_ts": ns(entry), "entry_ts": ns(entry), "exit_ts": ns(entry) + 3600 * 10**9,
                       "side": "long", "lots": 0.1, "entry_price": 2400.0, "exit_price": 2400.0,
                       "sl": 2390.0, "tp": None, "exit_reason": "tp" if p > 0 else "sl",
                       "entry_kind": "market", "tag": "", "gross_pnl": p + 1, "commission": 1.0,
                       "swap": 0.0, "spread_cost": 2.0, "slippage_cost": 0.2, "net_pnl": p,
                       "r_multiple": p / 100, "bars_held": 60})
        eq_ts.append(entry + timedelta(hours=1))
        eq.append(bal)
    return BacktestResult(_trades_frame(trades),
                          pl.DataFrame({"ts": eq_ts, "equity": eq}), {"decisions": 1}, "", "",
                          "M1", "M15", 10_000.0)


def test_summary_metrics():
    m = compute_metrics(_result([100, -50, 200, -100, 50]), SESSIONS)
    assert m["trades"] == 5
    assert m["net_profit"] == 200
    assert m["gross_profit"] == 350 and m["gross_loss"] == -150
    assert m["profit_factor"] == pytest.approx(350 / 150, abs=1e-3)
    assert m["win_rate_pct"] == 60.0
    assert m["avg_trade"] == 40
    # equity 10000 -> 10100 -> 10050 -> 10250 -> 10150 -> 10200: max DD 100 from 10250
    assert m["max_drawdown"] == 100
    assert m["costs"]["commission"] == 5.0
    assert m["sharpe"] is not None


def test_no_losses_gives_no_profit_factor():
    assert compute_metrics(_result([10, 20]), SESSIONS)["profit_factor"] is None


def test_breakdowns_by_weekday_and_session():
    b = breakdowns(_result([100, -50, 200, -100, 50]), SESSIONS)   # Mon..Fri, 09:00 UTC
    assert b["weekday"]["weekday"].to_list() == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert b["session"]["session"].to_list() == ["london"]          # 09:00 UTC = 09:00 London
    assert b["month"]["net"].to_list() == [200]
