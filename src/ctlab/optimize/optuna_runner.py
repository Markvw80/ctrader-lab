"""Optuna search over a strategy's declared parameters on one data period."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import optuna
import polars as pl

from ctlab.engine.backtest import BacktestResult, EngineConfig, prepare_market, run_backtest
from ctlab.engine.runner import Inputs
from ctlab.optimize.objective import REJECTED, objective_value
from ctlab.report.metrics import compute_metrics
from ctlab.strategy.base import Strategy
from ctlab.strategy.params import Param

optuna.logging.set_verbosity(optuna.logging.WARNING)


@dataclass
class Evaluator:
    """Backtests a strategy on a fixed period, reusing the prepared (resampled) bars."""

    strategy_cls: type[Strategy]
    inp: Inputs
    start: datetime | None        # first tradable time (earlier bars are warmup)
    end: datetime | None
    warmup_days: int = 10
    objective: str = "sharpe"
    min_trades: int = 0

    def __post_init__(self):
        bars = self.inp.bars
        if self.start is not None:
            bars = bars.filter(pl.col("ts") >= self.start - timedelta(days=self.warmup_days))
        if self.end is not None:
            bars = bars.filter(pl.col("ts") < self.end)
        self.bars = bars
        self.prepared = prepare_market(bars, self.strategy_cls.timeframe, self.inp.sessions,
                                       self.inp.exec_timeframe)

    def run(self, params: dict, initial_balance: float | None = None) -> BacktestResult:
        cfg = EngineConfig(**{**self.inp.cfg.__dict__, "trade_from": self.start})
        if initial_balance is not None:
            cfg.initial_balance = initial_balance
        return run_backtest(self.strategy_cls(**params), self.bars, self.inp.cost, self.inp.sessions,
                            cfg, self.inp.exec_timeframe, prepared=self.prepared)

    def score(self, params: dict) -> tuple[float, dict]:
        res = self.run(params)
        m = compute_metrics(res, self.inp.sessions)
        if m["trades"] < self.min_trades:
            return REJECTED, m
        v = objective_value(m, self.objective, res.initial_balance)
        return (v if math.isfinite(v) else REJECTED), m


def suggest(trial: optuna.Trial, name: str, p: Param):
    if p.kind == "int":
        return trial.suggest_int(name, int(p.low), int(p.high), step=int(p.step or 1))
    if p.kind == "float":
        if p.step:
            n = math.floor((p.high - p.low) / p.step + 1e-9)
            v = trial.suggest_float(name, p.low, p.low + n * p.step, step=p.step)
            return round(v, 10)
        return trial.suggest_float(name, p.low, p.high, log=p.log)
    return trial.suggest_categorical(name, list(p.choices))


SUMMARY_KEYS = ("trades", "net_profit", "return_pct", "profit_factor", "win_rate_pct",
                "max_drawdown_pct", "sharpe")


def optimize(ev: Evaluator, n_trials: int, seed: int, storage: Path | None = None,
             study_name: str = "study", progress=None) -> optuna.Study:
    specs = ev.strategy_cls.param_specs()
    sampler = optuna.samplers.TPESampler(seed=seed)
    study = optuna.create_study(
        direction="maximize", sampler=sampler, study_name=study_name,
        storage=None if storage is None else f"sqlite:///{storage}", load_if_exists=False)
    # always evaluate the declared defaults as a reference point
    study.enqueue_trial({k: p.default for k, p in specs.items()})

    def objective(trial: optuna.Trial) -> float:
        params = {k: suggest(trial, k, p) for k, p in specs.items()}
        trial.set_user_attr("params", params)
        value, m = ev.score(params)
        for k in SUMMARY_KEYS:
            trial.set_user_attr(k, m[k])
        trial.set_user_attr("rejected", value == REJECTED)
        if progress:
            progress(trial.number + 1, n_trials)
        return value

    study.optimize(objective, n_trials=n_trials, n_jobs=1, gc_after_trial=False)
    return study


def best_valid(study: optuna.Study) -> optuna.trial.FrozenTrial | None:
    ok = [t for t in study.trials if t.value is not None and not t.user_attrs.get("rejected")]
    return max(ok, key=lambda t: t.value) if ok else None


def best_params(trial: optuna.trial.FrozenTrial) -> dict:
    """Parameters exactly as passed to the strategy (rounded to their step grid)."""
    return dict(trial.user_attrs.get("params") or trial.params)


def trials_frame(study: optuna.Study) -> pl.DataFrame:
    rows = []
    for t in study.trials:
        rows.append({"trial": t.number, "value": t.value, **{f"p_{k}": v for k, v in t.params.items()},
                     **{k: t.user_attrs.get(k) for k in (*SUMMARY_KEYS, "rejected")}})
    return pl.DataFrame(rows, infer_schema_length=None)
