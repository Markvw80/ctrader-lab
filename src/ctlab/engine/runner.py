"""Glue between config, stored data and the engine (shared by CLI, optimizer and web)."""

from dataclasses import dataclass
from datetime import datetime

import polars as pl

from ctlab.config import settings, symbol_spec
from ctlab.data.loader import load_bars
from ctlab.engine.backtest import BacktestResult, EngineConfig, run_backtest
from ctlab.engine.costs import CostModel
from ctlab.strategy.base import Strategy


@dataclass
class Inputs:
    symbol: str
    bars: pl.DataFrame
    cost: CostModel
    sessions: dict
    cfg: EngineConfig
    exec_timeframe: str
    spread_stat: str
    spec: dict


def engine_config(overrides: dict | None = None) -> EngineConfig:
    acc = settings()["account"]
    cfg = EngineConfig(
        initial_balance=float(acc["initial_balance"]),
        risk_per_trade_pct=float(acc["risk_per_trade_pct"]),
        daily_loss_limit_pct=acc.get("daily_loss_limit_pct"),
        max_leverage=float(acc.get("max_leverage", 20)),
    )
    for k, v in (overrides or {}).items():
        if v is not None:
            setattr(cfg, k, v)
    return cfg


def load_inputs(symbol: str, start: datetime | None, end: datetime | None,
                exec_timeframe: str | None = None, spread_stat: str | None = None,
                cfg_overrides: dict | None = None) -> Inputs:
    bt = settings().get("backtest", {})
    etf = (exec_timeframe or bt.get("exec_timeframe", "M1")).upper()
    stat = spread_stat or bt.get("spread_stat", "median")
    spec = symbol_spec(symbol)
    bars = load_bars(symbol, etf, start, end)
    if bars.height == 0:
        raise ValueError(f"No {symbol} {etf} data in range {start} - {end}")
    return Inputs(symbol, bars, CostModel.from_spec(spec, stat), spec["sessions"],
                  engine_config(cfg_overrides), etf, stat, spec)


def backtest(strategy: Strategy, inp: Inputs, bars: pl.DataFrame | None = None) -> BacktestResult:
    return run_backtest(strategy, inp.bars if bars is None else bars, inp.cost, inp.sessions,
                        inp.cfg, inp.exec_timeframe)
