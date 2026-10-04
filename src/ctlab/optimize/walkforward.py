"""Walk-forward: optimize in-sample, test the winner out-of-sample, roll forward."""

from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from ctlab.engine.backtest import BacktestResult, _trades_frame
from ctlab.engine.runner import Inputs
from ctlab.optimize.objective import annual_return_pct
from ctlab.optimize.optuna_runner import Evaluator, best_params, best_valid, optimize, trials_frame
from ctlab.optimize.robustness import sensitivity, verdict
from ctlab.optimize.splits import Window, walkforward_windows
from ctlab.report.metrics import compute_metrics
from ctlab.strategy.base import Strategy


def _years(a, b) -> float:
    return (b - a).total_seconds() / (365.25 * 86400)


@dataclass
class WalkForwardResult:
    windows: list[dict]
    oos_trades: pl.DataFrame
    oos_equity: pl.DataFrame
    oos_metrics: dict
    is_annual_pct: float
    oos_annual_pct: float
    sensitivity: dict | None
    verdict: dict
    trials: pl.DataFrame
    research_range: tuple
    extra: dict = field(default_factory=dict)


def run_walkforward(strategy_cls: type[Strategy], inp: Inputs, research_end, s: dict,
                    n_trials: int, storage: Path | None = None, log=print) -> WalkForwardResult:
    bars = inp.bars.filter(pl.col("ts") < research_end)
    start = bars["ts"].min()
    wins: list[Window] = walkforward_windows(start, research_end, s["wf_in_sample_months"],
                                             s["wf_out_of_sample_months"], s.get("wf_anchored", False))
    research = Inputs(**{**inp.__dict__, "bars": bars})
    balance = inp.cfg.initial_balance
    rows, oos_t, oos_eq, all_trials = [], [], [], []
    is_ret, is_years, last_best = 0.0, 0.0, None

    for w in wins:
        log(f"window {w.n}/{len(wins)}: IS {w.is_start:%Y-%m-%d}..{w.is_end:%Y-%m-%d}  "
            f"OOS {w.oos_start:%Y-%m-%d}..{w.oos_end:%Y-%m-%d}")
        is_ev = Evaluator(strategy_cls, research, w.is_start, w.is_end, s["warmup_days"],
                          s["objective"], s["min_trades_per_window"])
        study = optimize(is_ev, n_trials, s["seed"] + w.n, storage, f"window_{w.n}")
        all_trials.append(trials_frame(study).with_columns(pl.lit(w.n).alias("window")))
        best = best_valid(study)
        row = {"window": w.n, "is_start": w.is_start, "is_end": w.is_end, "oos_start": w.oos_start,
               "oos_end": w.oos_end, "is_rejected": best is None}
        if best is None:
            log("  no valid parameters in-sample (too few trades); window skipped")
            rows.append({**row, "params": None, "is_objective": None, "is_net": None,
                         "is_trades": None, "is_sharpe": None, "oos_net": 0.0, "oos_trades": 0,
                         "oos_sharpe": None, "oos_return_pct": 0.0})
            continue
        params = best_params(best)
        last_best = (params, is_ev)
        yrs = _years(w.is_start, w.is_end)
        is_ret += best.user_attrs["return_pct"]
        is_years += yrs

        oos_ev = Evaluator(strategy_cls, research, w.oos_start, w.oos_end, s["warmup_days"],
                           s["objective"], 0)
        res = oos_ev.run(params, initial_balance=balance)
        m = compute_metrics(res, inp.sessions)
        balance = res.stats["final_balance"]
        oos_t.append(res.trades.with_columns(pl.lit(w.n).alias("window")))
        oos_eq.append(res.equity)
        rows.append({**row, "params": params, "is_objective": best.value,
                     "is_net": best.user_attrs["net_profit"], "is_trades": best.user_attrs["trades"],
                     "is_sharpe": best.user_attrs["sharpe"], "oos_net": m["net_profit"],
                     "oos_trades": m["trades"], "oos_sharpe": m["sharpe"],
                     "oos_return_pct": m["return_pct"]})
        log(f"  IS  {best.user_attrs['trades']:>4} trades  net {best.user_attrs['net_profit']:>10.2f}"
            f"  sharpe {best.user_attrs['sharpe']}")
        log(f"  OOS {m['trades']:>4} trades  net {m['net_profit']:>10.2f}  sharpe {m['sharpe']}")

    trades = pl.concat(oos_t) if oos_t else pl.DataFrame()
    equity = pl.concat(oos_eq) if oos_eq else pl.DataFrame({"ts": [], "equity": []})
    combined = BacktestResult(trades if oos_t else _empty_trades(), equity, {}, "", "",
                              inp.exec_timeframe, strategy_cls.timeframe, inp.cfg.initial_balance)
    oos_m = compute_metrics(combined, inp.sessions)
    valid = [w for w in wins if any(r["window"] == w.n and not r["is_rejected"] for r in rows)]
    oos_years = sum(_years(w.oos_start, w.oos_end) for w in valid)
    is_annual = is_ret / is_years if is_years else 0.0
    oos_annual = annual_return_pct(oos_m, oos_years) if oos_years else 0.0

    sens = None
    if last_best is not None:
        params, ev = last_best
        log("sensitivity analysis on the most recent window's parameters ...")
        sens = sensitivity(strategy_cls, params, ev.score, s["sensitivity_min_stability"])
    v = verdict(is_annual, oos_annual, rows, oos_m["trades"], sens, s)
    return WalkForwardResult(rows, trades, equity, oos_m, round(is_annual, 3), round(oos_annual, 3),
                             sens, v, pl.concat(all_trials, how="diagonal_relaxed") if all_trials
                             else pl.DataFrame(), (str(start), str(research_end)))


def _empty_trades() -> pl.DataFrame:
    return _trades_frame([])
