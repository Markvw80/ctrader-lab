"""Cost calculations. Every expected number is computed by hand in the comments."""

import pytest

from ctlab.config import symbol_spec
from ctlab.engine.costs import CostModel
from ctlab.engine.sizing import lots_for_risk
from tests.engine_helpers import ONE_LOT, cost_model, flat, run

# --- commission ----------------------------------------------------------------------------

def test_commission_per_million_notional():
    c = cost_model(commission_type="usd_per_million_notional", commission_value=30)
    # 1 lot * 100 oz * 2400 = 240,000 notional -> 30 * 0.24 = 7.20 per side
    assert c.commission(1.0, 2400) == pytest.approx(7.20)
    assert c.commission(0.5, 2400) == pytest.approx(3.60)


def test_commission_per_lot_and_percentage():
    assert cost_model(commission_value=3.5).commission(2.0, 2400) == pytest.approx(7.0)
    pct = cost_model(commission_type="percentage_of_value", commission_value=0.01)
    assert pct.commission(1.0, 2400) == pytest.approx(24.0)  # 0.01% of 240,000


# --- XAUUSD contract -----------------------------------------------------------------------

def test_xauusd_contract_and_pip_value():
    c = CostModel.from_spec(symbol_spec("XAUUSD", broker=False))
    assert c.contract_size == 100
    assert c.tick_size == 0.01
    assert c.pip_value_per_lot() == pytest.approx(10.0)    # 0.10 move * 100 oz
    assert c.slippage == pytest.approx(0.02)               # 2 ticks
    assert c.spread_default == pytest.approx(0.20)         # 20 points
    # 1.00 USD move on 1 lot = 100 USD
    assert 1.0 * c.contract_size == 100


# --- swap ----------------------------------------------------------------------------------

def test_swap_per_night_variants():
    assert cost_model(swap_long=-50).swap_per_night(1, 0.5, 2400) == pytest.approx(-25.0)
    pips = cost_model(swap_calculation="pips", swap_short=-3)
    assert pips.swap_per_night(-1, 1.0, 2400) == pytest.approx(-30.0)   # -3 * 0.1 * 100
    pct = cost_model(swap_calculation="percentage", swap_long=-5)
    assert pct.swap_per_night(1, 1.0, 2400) == pytest.approx(-5 / 100 * 240_000 / 360)


@pytest.mark.parametrize("from_day,to_day,nights", [
    ("2024-01-09", "2024-01-10", 1),   # Tue ends
    ("2024-01-10", "2024-01-11", 3),   # Wed ends: triple
    ("2024-01-12", "2024-01-15", 1),   # Fri ends; Sat/Sun no rollover
    ("2024-01-08", "2024-01-15", 7),   # Mon..Fri = 1+1+3+1+1
])
def test_swap_nights(from_day, to_day, nights):
    from datetime import date
    d = lambda s: (date.fromisoformat(s) - date(1970, 1, 1)).days
    assert cost_model().swap_nights(d(from_day), d(to_day)) == nights


# --- position sizing -----------------------------------------------------------------------

def test_sizing_rounds_down_and_includes_costs():
    c = cost_model(commission_type="usd_per_million_notional", commission_value=30)
    # risk 0.5% of 10,000 = 50. Per lot: (5.00 SL + 0.02 slip) * 100 + 2 * 7.20 = 516.40
    # 50 / 516.40 = 0.0968 -> 0.09
    assert lots_for_risk(c, 10_000, 0.5, 5.0, 2400, 20) == 0.09


def test_sizing_below_min_lot_is_zero():
    assert lots_for_risk(cost_model(), 10_000, 0.5, 200.0, 2400, 20) == 0.0


def test_sizing_leverage_cap():
    # tiny stop -> huge size; cap: 10,000 * 20 / (100 * 2400) = 0.833 -> 0.83
    assert lots_for_risk(cost_model(), 10_000, 0.5, 0.01, 2400, 20) == 0.83


# --- complete trades -----------------------------------------------------------------------

def test_long_round_trip_costs_exact():
    rows = flat(4) + [(2410, 2410, 2410, 2410)] + flat(1, 2410)
    res, _ = run({0: lambda ctx: ctx.buy(10.0), 3: lambda ctx: ctx.close()}, rows)
    t = res.trades.row(0, named=True)
    assert t["lots"] == 1.0
    assert t["entry_price"] == pytest.approx(2400.22)      # open 2400 + spread 0.20 + slip 0.02
    assert t["exit_price"] == pytest.approx(2409.98)       # open 2410 - slip 0.02 (sell at bid)
    assert t["gross_pnl"] == pytest.approx(976.0)          # 9.76 * 100
    assert t["commission"] == pytest.approx(7.0)           # 3.5 per side
    assert t["net_pnl"] == pytest.approx(969.0)
    assert t["spread_cost"] == pytest.approx(20.0)         # 0.20 * 100 oz, paid at entry
    assert t["slippage_cost"] == pytest.approx(4.0)        # 0.02 * 100, entry and exit
    assert res.stats["final_balance"] == pytest.approx(100_969.0)


def test_short_round_trip_costs_exact():
    rows = flat(4) + [(2410, 2410, 2410, 2410)] + flat(1, 2410)
    res, _ = run({0: lambda ctx: ctx.sell(20.0), 3: lambda ctx: ctx.close()}, rows,
                 cfg=ONE_LOT.__class__(100_000, 2.009, None, 20))   # (20.02*100+7)=2009 -> 1 lot
    t = res.trades.row(0, named=True)
    assert t["lots"] == 1.0
    assert t["entry_price"] == pytest.approx(2399.98)      # sell at bid open - slip
    assert t["exit_price"] == pytest.approx(2410.22)       # buy back at ask: 2410 + 0.20 + 0.02
    assert t["gross_pnl"] == pytest.approx(-1024.0)
    assert t["net_pnl"] == pytest.approx(-1031.0)
    assert t["spread_cost"] == pytest.approx(20.0)         # paid at exit for shorts


def test_triple_swap_across_wednesday_rollover():
    from datetime import UTC, datetime
    # Wed 2024-01-10 21:57 UTC = 16:57 NY; rollover at 22:00 UTC (17:00 NY winter)
    start = datetime(2024, 1, 10, 21, 57, tzinfo=UTC)
    rows = flat(8)
    res, _ = run({0: lambda ctx: ctx.buy(10.0), 6: lambda ctx: ctx.close()}, rows,
                 cost=cost_model(swap_long=-10.0), start=start)
    t = res.trades.row(0, named=True)
    assert t["swap"] == pytest.approx(-30.0)               # -10 per lot * 1 lot * 3 nights
    assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - 7.0 - 30.0)


def test_no_swap_intraday():
    res, _ = run({0: lambda ctx: ctx.buy(10.0), 5: lambda ctx: ctx.close()}, flat(8),
                 cost=cost_model(swap_long=-10.0))
    assert res.trades["swap"][0] == 0.0
