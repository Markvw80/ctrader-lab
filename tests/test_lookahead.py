"""No look-ahead: decisions use closed bars only and execute on the next bar."""

from datetime import timedelta

import numpy as np
import pytest

from ctlab.engine.costs import CostModel
from ctlab.strategy.base import Strategy
from ctlab.strategy.causality import check_engine, check_prepare, check_strategy
from tests.conftest import make_m1
from tests.engine_helpers import T0, cost_model, flat, run


def test_strategy_sees_only_bars_up_to_current():
    rows = [(2400 + i, 2401 + i, 2399 + i, 2400.5 + i) for i in range(10)]
    _, strat = run({}, rows)
    for s in strat.seen:
        assert s["n_visible"] == s["i"] + 1
        assert s["last_close"] == rows[s["i"]][3]


def test_market_order_fills_at_next_bar_open_not_signal_close():
    rows = [(2400, 2401, 2399, 2400.5), (2405, 2406, 2404, 2405.5)] + flat(3, 2405)
    res, _ = run({0: lambda ctx: ctx.buy(10.0)}, rows)
    t = res.trades.row(0, named=True)
    assert t["entry_ts"] == T0 + timedelta(minutes=1)
    assert t["entry_price"] == pytest.approx(2405 + 0.20 + 0.02)   # bar 1 OPEN, not bar 0 close
    assert t["signal_ts"] == T0


def test_stop_order_never_fills_on_the_bar_that_produced_it():
    rows = [
        (2400, 2405, 2399, 2400),   # bar 0 trades through 2402 -> order placed at its close
        (2400, 2401, 2399, 2400),   # ask high 2401.20 < 2402: no fill
        (2401, 2403, 2400, 2402),   # ask high 2403.20 >= 2402: fill
    ] + flat(2, 2402)
    res, _ = run({0: lambda ctx: ctx.buy_stop(2402.0, 10.0)}, rows)
    t = res.trades.row(0, named=True)
    assert t["entry_ts"] == T0 + timedelta(minutes=2)
    assert t["entry_price"] == pytest.approx(2402.02)              # stop price + slippage


def test_higher_timeframe_decision_executes_after_bar_close():
    # M5 strategy on M1 execution: first M5 bar is 10:00-10:04, decision applies at 10:05 open
    rows = [(2400 + i, 2400 + i, 2400 + i, 2400 + i) for i in range(15)]
    res, strat = run({0: lambda ctx: ctx.buy(10.0)}, rows, timeframe="M5")
    t = res.trades.row(0, named=True)
    assert t["entry_ts"] == T0 + timedelta(minutes=5)
    assert t["entry_price"] == pytest.approx(2405 + 0.22)
    assert strat.seen[0]["last_close"] == 2404                    # M5 close = last M1 close


def test_future_prices_do_not_change_earlier_decisions():
    base = [(2400 + np.sin(i), 2401 + np.sin(i), 2399 + np.sin(i), 2400 + np.sin(i)) for i in range(30)]
    changed = base[:15] + [(3000, 3001, 2999, 3000)] * 15
    script = {i: (lambda ctx: ctx.buy(5.0) if ctx.position is None else None) for i in range(30)}
    _, s1 = run(script, base)
    _, s2 = run(script, changed)
    assert s1.seen[:14] == s2.seen[:14]


# --- the automatic checker must itself detect leaks ----------------------------------------

class LeakyPrepare(Strategy):
    name = "_leaky_prepare"
    timeframe = "M15"

    def prepare(self, bars):
        return {"next_close": bars["close"].shift(-1).fill_null(np.nan).to_numpy()}

    def on_bar(self, ctx):
        nc = ctx.now("next_close")
        if ctx.position is None and not np.isnan(nc) and nc > ctx.bars.close[-1]:
            ctx.buy(5.0, 5.0)
        elif ctx.position is not None:
            ctx.close()


class HonestMomentum(Strategy):
    name = "_honest"
    timeframe = "M15"

    def prepare(self, bars):
        return {"prev_close": bars["close"].shift(1).fill_null(np.nan).to_numpy()}

    def on_bar(self, ctx):
        pc = ctx.now("prev_close")
        if ctx.position is None and not np.isnan(pc) and ctx.bars.close[-1] > pc:
            ctx.buy(5.0, 5.0)
        elif ctx.position is not None:
            ctx.close()


@pytest.fixture(scope="module")
def week():
    from datetime import UTC, datetime
    return make_m1(datetime(2024, 1, 8, tzinfo=UTC), datetime(2024, 1, 20, tzinfo=UTC), seed=11)


def _cost():
    return cost_model(commission_type="usd_per_million_notional", commission_value=30)


def test_checker_flags_leaky_prepare(week):
    from ctlab.data.resample import resample
    from ctlab.engine.calendar import add_calendar
    rep = check_prepare(LeakyPrepare(), add_calendar(resample(week, "M15"), {}))
    assert not rep.ok and "next_close" in rep.problems[0]


def test_checker_flags_leak_in_trades(week):
    rep = check_engine(LeakyPrepare, {}, week, _cost(), {})
    assert not rep.ok


def test_checker_passes_honest_strategy(week):
    rep = check_strategy(HonestMomentum, {}, week, _cost(), {})
    assert rep.ok, rep.problems


@pytest.mark.parametrize("name", ["session_breakout", "mean_reversion"])
def test_reference_strategies_are_causal(name, week):
    from ctlab.config import symbol_spec
    from ctlab.strategy.base import get_strategy
    spec = symbol_spec("XAUUSD", broker=False)
    rep = check_strategy(get_strategy(name), {}, week, CostModel.from_spec(spec), spec["sessions"])
    assert rep.ok, rep.problems


