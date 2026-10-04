"""Optimization, walk-forward splits, holdout isolation and the robustness verdict."""

import json
from datetime import UTC, datetime
from itertools import pairwise

import numpy as np
import pytest

from ctlab.config import env, symbol_spec
from ctlab.engine.backtest import EngineConfig
from ctlab.engine.costs import CostModel
from ctlab.engine.runner import Inputs
from ctlab.optimize.optuna_runner import Evaluator, best_params, best_valid, optimize
from ctlab.optimize.robustness import neighbours, sensitivity, verdict
from ctlab.optimize.splits import add_months, holdout_start, walkforward_windows
from ctlab.strategy.base import Strategy
from ctlab.strategy.params import ChoiceParam, FloatParam, IntParam
from tests.conftest import make_m1

D = lambda *a: datetime(*a, tzinfo=UTC)

SETTINGS = {"min_trades_oos": 10, "not_robust_oos_is_ratio": 0.5,
            "min_profitable_oos_windows_pct": 50}


# --- windows & holdout ---------------------------------------------------------------------

def test_add_months():
    assert add_months(D(2023, 11, 1), 3) == D(2024, 2, 1)


def test_rolling_windows_are_contiguous_and_never_overlap_oos():
    w = walkforward_windows(D(2020, 1, 1), D(2023, 1, 1), 12, 3)
    assert w[0].is_start == D(2020, 1, 1) and w[0].oos_start == D(2021, 1, 1)
    for a, b in pairwise(w):
        assert b.oos_start == a.oos_end                     # OOS periods tile the timeline
        assert b.is_start == add_months(a.is_start, 3)      # rolling
    assert all(x.is_end == x.oos_start for x in w)          # OOS strictly after IS
    assert w[-1].oos_end <= D(2023, 1, 1)
    assert len(w) == 8


def test_anchored_windows_keep_start():
    w = walkforward_windows(D(2020, 1, 1), D(2022, 1, 1), 6, 6, anchored=True)
    assert {x.is_start for x in w} == {D(2020, 1, 1)}


def test_short_last_window_kept_only_if_at_least_half():
    assert walkforward_windows(D(2020, 1, 1), D(2021, 2, 15), 12, 3)[-1].oos_end == D(2021, 2, 15)
    assert len(walkforward_windows(D(2020, 1, 1), D(2021, 1, 20), 12, 3)) == 0


def test_holdout_start_is_fixed_after_first_use(tmp_path, monkeypatch):
    monkeypatch.setenv("CTLAB_DATA_DIR", str(tmp_path))
    env.cache_clear()
    try:
        h1 = holdout_start("XAUUSD", D(2020, 1, 1), D(2025, 1, 1), 20)
        assert D(2023, 12, 1) < h1 < D(2024, 2, 1)
        # more data arrives later: the boundary must not move
        h2 = holdout_start("XAUUSD", D(2020, 1, 1), D(2026, 6, 1), 20)
        assert h2 == h1
        assert json.loads((tmp_path / "research" / "holdout_XAUUSD.json").read_text())["pct"] == 20
    finally:
        env.cache_clear()


# --- sensitivity neighbours ----------------------------------------------------------------

def test_neighbours_respect_bounds_and_grid():
    p = IntParam(20, 5, 100, step=5)
    assert set(neighbours(p, 20)) == {15, 25, 10, 30}      # one step (5) and 10% of range (9.5->10)
    assert set(neighbours(p, 5)) == {10, 15}             # lower bound clipped
    f = FloatParam(1.0, 0.5, 2.0, step=0.1)
    assert all(0.5 <= v <= 2.0 for v in neighbours(f, 1.0))
    assert neighbours(ChoiceParam("a", ("a", "b", "c")), "a") == ["b", "c"]


# --- evaluator: warmup and period isolation -------------------------------------------------

class EveryBar(Strategy):
    name = "_every_bar"
    timeframe = "M15"
    sl = FloatParam(3.0, 1.0, 10.0, step=0.5)

    def on_bar(self, ctx):
        if ctx.position is None:
            ctx.buy(self.sl, self.sl)


def _inputs(bars):
    spec = symbol_spec("XAUUSD", broker=False)
    return Inputs("XAUUSD", bars, CostModel.from_spec(spec), spec["sessions"],
                  EngineConfig(10_000, 0.5, None, 20), "M1", "median", spec)


@pytest.fixture(scope="module")
def two_months():
    return make_m1(D(2024, 1, 1), D(2024, 3, 1), seed=21)


def test_evaluator_trades_only_inside_its_period(two_months):
    ev = Evaluator(EveryBar, _inputs(two_months), D(2024, 1, 15), D(2024, 2, 1), warmup_days=5)
    assert ev.bars["ts"].min() >= D(2024, 1, 10)            # warmup only
    assert ev.bars["ts"].max() < D(2024, 2, 1)              # nothing after the period
    t = ev.run({"sl": 3.0}).trades
    assert t.height > 0
    assert (t["entry_ts"] >= D(2024, 1, 15)).all()
    assert (t["exit_ts"] < D(2024, 2, 1)).all()


def test_min_trades_rejects(two_months):
    ev = Evaluator(EveryBar, _inputs(two_months), None, None, objective="sharpe", min_trades=10**6)
    value, m = ev.score({"sl": 3.0})
    assert value == -1e6 and m["trades"] > 0


class Planted(Strategy):
    """Only k == 7 trades; everything else does nothing -> optimizer must find k = 7."""
    name = "_planted"
    timeframe = "M15"
    k = IntParam(1, 1, 10)

    def on_bar(self, ctx):
        if self.k == 7 and ctx.position is None and ctx.bars.close[-1] > ctx.bars.open[-1]:
            ctx.buy(2.0, 2.0)


def test_optimizer_finds_and_rounds_parameters(two_months, tmp_path):
    ev = Evaluator(Planted, _inputs(two_months), None, None, objective="net_profit", min_trades=1)
    study = optimize(ev, 25, seed=1, storage=tmp_path / "o.db")
    best = best_valid(study)
    assert best is not None and best_params(best) == {"k": 7}
    assert (tmp_path / "o.db").exists()


# --- verdict --------------------------------------------------------------------------------

def _w(is_net, oos_net, oos_trades=20, rejected=False):
    return {"is_rejected": rejected, "is_net": is_net, "oos_net": oos_net, "oos_trades": oos_trades}


def test_verdict_robust_when_oos_holds_up():
    v = verdict(10.0, 7.0, [_w(100, 50), _w(80, 30), _w(90, -5)], 60, None, SETTINGS)
    assert v["robust"] and v["label"] == "ROBUST" and v["oos_is_ratio"] == 0.7


def test_verdict_not_robust_when_oos_much_worse():
    v = verdict(10.0, 3.0, [_w(100, 10), _w(80, 5)], 40, None, SETTINGS)
    assert not v["robust"] and any("< 50%" in r for r in v["reasons"])


def test_verdict_not_robust_when_oos_losing_or_too_few_trades():
    v = verdict(10.0, -1.0, [_w(100, -10, 3)], 3, None, SETTINGS)
    assert v["label"] == "NOT ROBUST"
    assert any("losing" in r for r in v["reasons"]) and any("too few" in r for r in v["reasons"])


def test_verdict_includes_sensitivity_and_losing_in_sample():
    sens = {"sensitive": True, "reasons": ["objective drops"]}
    v = verdict(10.0, 9.0, [_w(-5, 20), _w(50, 20)], 40, sens, SETTINGS)
    assert any("sensitivity" in r for r in v["reasons"])
    assert any("even the best in-sample" in r for r in v["reasons"])


def test_sensitivity_flags_a_spike_optimum():
    def score(p):   # sharp peak at x=5, nothing around it
        v = 10.0 if p["x"] == 5 else -1.0
        return v, {"net_profit": v, "trades": 50}

    class S(Strategy):
        name = "_s"
        x = IntParam(5, 0, 10)

    rep = sensitivity(S, {"x": 5}, score, 0.5)
    assert rep["sensitive"]

    def flat_score(p):
        return 2.0 - 0.01 * abs(p["x"] - 5), {"net_profit": 1.0, "trades": 50}
    assert not sensitivity(S, {"x": 5}, flat_score, 0.5)["sensitive"]


def test_research_data_never_reaches_holdout(two_months):
    """Walk-forward evaluators are built only from bars before the research end."""
    from ctlab.optimize.walkforward import run_walkforward
    s = {"wf_in_sample_months": 1, "wf_out_of_sample_months": 1, "warmup_days": 3,
         "objective": "net_profit", "min_trades_per_window": 1, "seed": 1,
         "sensitivity_min_stability": 0.5, **SETTINGS}
    research_end = D(2024, 2, 20)
    seen_max = []
    orig = Evaluator.__post_init__

    def spy(self):
        orig(self)
        seen_max.append(self.bars["ts"].max())
    Evaluator.__post_init__ = spy
    try:
        wf = run_walkforward(EveryBar, _inputs(two_months), research_end, s, 3, log=lambda *_: None)
    finally:
        Evaluator.__post_init__ = orig
    assert seen_max and max(seen_max) < research_end
    assert wf.verdict["label"] in ("ROBUST", "NOT ROBUST")
    assert np.all(wf.oos_trades["exit_ts"].to_numpy() < np.datetime64(research_end.replace(tzinfo=None)))
