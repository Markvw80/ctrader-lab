"""Event-driven backtest engine.

Timeline per execution bar k (normally M1), in this order:
  1. Trading-day change (17:00 New York): apply swap, reset daily loss limit.
  2. Strategy decisions for every strategy bar that CLOSED at or before the open of bar k.
  3. Execute what was decided: close / cancel / market orders fill at the OPEN of bar k;
     stop/limit orders become active from bar k on.
  4. Intrabar: pending order fills, then stop loss / take profit.
  5. Bar close: mark-to-market, daily loss limit check.

Prices: bars are BID. ask = bid + spread(NY hour). Longs buy at ask and sell at bid, shorts the
opposite. Conservative rules:
  - SL and TP touched in the same bar -> SL.
  - On the bar a stop/limit entry fills, only the SL is checked (intrabar order unknown).
  - Price opening beyond the SL fills at the open (gap), not at the SL.
  - Slippage (adverse) on market orders, stop entries and SL exits; none on TP/limit fills.
One position at a time. Opening a position cancels all other pending orders (OCO).
"""

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import polars as pl

from ctlab.data.resample import resample
from ctlab.data.schema import timeframe_minutes
from ctlab.engine.calendar import add_calendar
from ctlab.engine.costs import CostModel
from ctlab.engine.sizing import lots_for_risk
from ctlab.strategy.base import LONG, Context, OrderRequest, PositionInfo, Strategy

NS_PER_MIN = 60_000_000_000


@dataclass
class EngineConfig:
    initial_balance: float = 10_000.0
    risk_per_trade_pct: float = 0.5
    daily_loss_limit_pct: float | None = 2.0
    max_leverage: float = 20.0
    # Warmup: on_bar runs on earlier bars, but decisions on bars closing before this are dropped.
    trade_from: datetime | None = None


@dataclass
class _Pos:
    side: int
    lots: float
    units: float
    entry_price: float
    sl: float
    tp: float | None
    entry_k: int
    signal_ts: int
    tag: str
    kind: str
    risk_amount: float
    commission: float = 0.0
    swap: float = 0.0
    spread_cost: float = 0.0
    slippage_cost: float = 0.0


@dataclass
class _Pending:
    req: OrderRequest
    signal_ts: int
    expires: int | None


@dataclass
class BacktestResult:
    trades: pl.DataFrame
    equity: pl.DataFrame
    stats: dict
    start: str
    end: str
    exec_timeframe: str
    strategy_timeframe: str
    initial_balance: float
    params: dict = field(default_factory=dict)


def prepare_columns(strategy: Strategy, strat_bars: pl.DataFrame) -> dict[str, np.ndarray]:
    """Bar columns + calendar + strategy indicators as numpy arrays (all the same length)."""
    cols = {c: strat_bars[c].to_numpy() for c in strat_bars.columns if c != "ts"}
    cols["ts"] = strat_bars["ts"].dt.epoch("ns").to_numpy()
    for name, arr in strategy.prepare(strat_bars).items():
        arr = np.asarray(arr.to_numpy() if isinstance(arr, pl.Series) else arr)
        if len(arr) != strat_bars.height:
            raise ValueError(f"Indicator {name!r} has length {len(arr)}, expected {strat_bars.height}")
        cols[name] = arr
    return cols


def prepare_market(exec_bars: pl.DataFrame, strategy_timeframe: str, sessions: dict,
                   exec_timeframe: str = "M1") -> tuple[pl.DataFrame, pl.DataFrame]:
    """(execution bars, strategy bars), both with calendar columns. Cache this across runs."""
    stf, etf = strategy_timeframe.upper(), exec_timeframe.upper()
    if timeframe_minutes(stf) < timeframe_minutes(etf):
        raise ValueError(f"Strategy timeframe {stf} is smaller than execution timeframe {etf}")
    exec_bars = exec_bars.sort("ts")
    strat_bars = exec_bars if stf == etf else resample(exec_bars, stf)
    return add_calendar(exec_bars, sessions), add_calendar(strat_bars, sessions)


def run_backtest(strategy: Strategy, exec_bars: pl.DataFrame, cost: CostModel, sessions: dict,
                 cfg: EngineConfig | None = None, exec_timeframe: str = "M1",
                 prepared: tuple[pl.DataFrame, pl.DataFrame] | None = None) -> BacktestResult:
    cfg = cfg or EngineConfig()
    stf = strategy.timeframe.upper()
    etf = exec_timeframe.upper()
    if prepared is None:
        prepared = prepare_market(exec_bars, stf, sessions, etf)
    exec_bars, strat_bars = prepared
    trade_from_ns = None
    if cfg.trade_from is not None:
        trade_from_ns = int(pl.Series([cfg.trade_from]).dt.cast_time_unit("ns").dt.epoch("ns")[0])

    cols = prepare_columns(strategy, strat_bars)
    s_ts = cols["ts"]
    tf_ns = timeframe_minutes(stf) * NS_PER_MIN

    E_ts_np = exec_bars["ts"].dt.epoch("ns").to_numpy()
    dec = np.searchsorted(E_ts_np, s_ts + tf_ns, side="left")  # exec bar where decision j applies
    E_ts = E_ts_np.tolist()
    O = exec_bars["open"].to_list()
    H = exec_bars["high"].to_list()
    L = exec_bars["low"].to_list()
    C = exec_bars["close"].to_list()
    S = cost.spread_series(exec_bars["ts"]).tolist()
    TD = exec_bars["trading_day"].to_list()
    n, m = len(E_ts), len(s_ts)
    slip = cost.slippage
    limit = cfg.daily_loss_limit_pct

    balance = cfg.initial_balance
    pos: _Pos | None = None
    pendings: list[_Pending] = []
    q_orders: list[OrderRequest] = []
    q_close: str | None = None
    q_cancel = False
    q_signal_ts = 0
    halted = False
    cur_day: int | None = None
    day_start_equity = balance
    last_unreal = 0.0
    trades: list[dict] = []
    eq_ts: list[int] = []
    eq_val: list[float] = []
    stats = {"skipped_min_lot": 0, "blocked_daily_limit": 0, "ignored_in_position": 0,
             "expired_orders": 0, "daily_limit_hits": 0, "decisions": 0}

    def open_pos(side: int, fill: float, k: int, req: OrderRequest, slipped: bool, sig_ts: int) -> bool:
        nonlocal balance, pos
        lots = lots_for_risk(cost, balance, cfg.risk_per_trade_pct, req.sl_distance, fill,
                             cfg.max_leverage)
        if lots <= 0:
            stats["skipped_min_lot"] += 1
            return False
        units = lots * cost.contract_size
        comm = cost.commission(lots, fill)
        balance -= comm
        pos = _Pos(
            side=side, lots=lots, units=units, entry_price=fill,
            sl=fill - side * req.sl_distance,
            tp=None if req.tp_distance is None else fill + side * req.tp_distance,
            entry_k=k, signal_ts=sig_ts, tag=req.tag, kind=req.kind,
            risk_amount=req.sl_distance * units, commission=comm,
            spread_cost=S[k] * units if side == LONG else 0.0,
            slippage_cost=slip * units if slipped else 0.0,
        )
        return True

    def close_pos(k: int, price: float, reason: str, slipped: bool, exit_ts: int) -> None:
        nonlocal balance, pos
        p = pos
        gross = (price - p.entry_price) * p.side * p.units
        comm = cost.commission(p.lots, price)
        balance += gross - comm
        p.commission += comm
        if p.side != LONG:
            p.spread_cost += S[k] * p.units
        if slipped:
            p.slippage_cost += slip * p.units
        net = gross - p.commission + p.swap
        trades.append({
            "signal_ts": p.signal_ts, "entry_ts": E_ts[p.entry_k], "exit_ts": exit_ts,
            "side": "long" if p.side == LONG else "short", "lots": p.lots,
            "entry_price": p.entry_price, "exit_price": price, "sl": p.sl, "tp": p.tp,
            "exit_reason": reason, "entry_kind": p.kind, "tag": p.tag,
            "gross_pnl": gross, "commission": p.commission, "swap": p.swap,
            "spread_cost": p.spread_cost, "slippage_cost": p.slippage_cost, "net_pnl": net,
            "r_multiple": net / p.risk_amount if p.risk_amount else 0.0,
            "bars_held": k - p.entry_k + 1,
        })
        pos = None
        eq_ts.append(exit_ts)
        eq_val.append(balance)

    j = 0
    k = 0
    while k < n:
        t = E_ts[k]
        o, h, lo, c, s = O[k], H[k], L[k], C[k], S[k]

        # 1. trading-day change
        td = TD[k]
        if td != cur_day:
            if pos is not None and cur_day is not None:
                nights = cost.swap_nights(cur_day, td)
                if nights:
                    sw = cost.swap_per_night(pos.side, pos.lots, o) * nights
                    balance += sw
                    pos.swap += sw
            cur_day = td
            day_start_equity = balance + last_unreal
            halted = False
            eq_ts.append(t)
            eq_val.append(day_start_equity)

        # 2. strategy decisions (bars that closed before this exec bar opened)
        while j < m and dec[j] <= k:
            if j >= strategy.warmup_bars:
                info = None
                if pos is not None:
                    info = PositionInfo(pos.side, pos.lots, pos.entry_price, pos.sl, pos.tp,
                                        np.datetime64(E_ts[pos.entry_k], "ns"), pos.tag)
                ctx = Context(j, cols, info, bool(pendings), balance + last_unreal, halted)
                strategy.on_bar(ctx)
                stats["decisions"] += 1
                if trade_from_ns is not None and s_ts[j] + tf_ns < trade_from_ns:
                    j += 1      # warmup: strategy state only, no actions
                    continue
                if ctx.cancel_requested:
                    q_cancel = True
                if ctx.close_requested and q_close is None:
                    q_close = "signal"
                if ctx.orders:
                    q_orders.extend(ctx.orders)
                    q_signal_ts = int(s_ts[j])
            j += 1

        # 3. execute decisions at the open
        if q_cancel:
            pendings.clear()
        if q_close is not None and pos is not None:
            if pos.side == LONG:
                close_pos(k, o - slip, q_close, True, t)
            else:
                close_pos(k, o + s + slip, q_close, True, t)
        for req in q_orders:
            if halted:
                stats["blocked_daily_limit"] += 1
                continue
            if req.kind == "market":
                if pos is not None:
                    stats["ignored_in_position"] += 1
                    continue
                fill = o + s + slip if req.side == LONG else o - slip
                open_pos(req.side, fill, k, req, True, q_signal_ts)
            else:
                exp = None if req.expire_bars is None else q_signal_ts + (req.expire_bars + 1) * tf_ns
                pendings.append(_Pending(req, q_signal_ts, exp))
        q_orders = []
        q_close = None
        q_cancel = False

        if pendings:
            before = len(pendings)
            pendings = [p for p in pendings if p.expires is None or p.expires > t]
            stats["expired_orders"] += before - len(pendings)

        # 4. intrabar: pending fills, then SL/TP
        entered_now = False
        if pos is None and pendings:
            best = None
            for p in pendings:
                r = p.req
                P = r.price
                if r.side == LONG:
                    ref = o + s
                    if r.kind == "stop" and h + s >= P:
                        fill = max(P, ref) + slip
                    elif r.kind == "limit" and lo + s <= P:
                        fill = min(P, ref)
                    else:
                        continue
                else:
                    ref = o
                    if r.kind == "stop" and lo <= P:
                        fill = min(P, ref) - slip
                    elif r.kind == "limit" and h >= P:
                        fill = max(P, ref)
                    else:
                        continue
                dist = abs(P - ref)
                if best is None or dist < best[0]:
                    best = (dist, p, fill)
            if best is not None:
                _, p, fill = best
                if open_pos(p.req.side, fill, k, p.req, p.req.kind == "stop", p.signal_ts):
                    entered_now = True
                    pendings.clear()
                else:
                    pendings.remove(p)

        if pos is not None:
            if pos.side == LONG:
                if lo <= pos.sl:
                    px = pos.sl if (entered_now or o > pos.sl) else o
                    close_pos(k, px - slip, "sl", True, t)
                elif not entered_now and pos.tp is not None and h >= pos.tp:
                    close_pos(k, pos.tp, "tp", False, t)
            else:
                if h + s >= pos.sl:
                    px = pos.sl if (entered_now or o + s < pos.sl) else o + s
                    close_pos(k, px + slip, "sl", True, t)
                elif not entered_now and pos.tp is not None and lo + s <= pos.tp:
                    close_pos(k, pos.tp, "tp", False, t)

        # 5. bar close: mark-to-market, daily loss limit
        if pos is not None:
            if pos.side == LONG:
                last_unreal = (c - pos.entry_price) * pos.units
            else:
                last_unreal = (pos.entry_price - (c + s)) * pos.units
            eq_ts.append(t)
            eq_val.append(balance + last_unreal)
        else:
            last_unreal = 0.0
        if (limit and not halted and day_start_equity > 0
                and balance + last_unreal - day_start_equity <= -limit / 100 * day_start_equity):
            halted = True
            stats["daily_limit_hits"] += 1
            pendings.clear()
            if pos is not None:
                q_close = "daily_limit"

        # advance; skip bars where nothing can happen
        k += 1
        if pos is None and not pendings and q_close is None:
            nxt = int(dec[j]) if j < m else n
            k = max(k, nxt)

    if pos is not None:
        kl = n - 1
        px = C[kl] if pos.side == LONG else C[kl] + S[kl]
        close_pos(kl, px, "end_of_data", False, E_ts[kl])

    trades_df = _trades_frame(trades)
    equity_df = (
        pl.DataFrame({"ts_ns": eq_ts, "equity": eq_val}, schema={"ts_ns": pl.Int64, "equity": pl.Float64})
        .filter(pl.col("ts_ns") >= (trade_from_ns or 0))
        .with_columns(ts=pl.from_epoch("ts_ns", time_unit="ns").dt.replace_time_zone("UTC"))
        .select("ts", "equity")
    )
    stats["final_balance"] = balance
    start = str(exec_bars["ts"].min()) if cfg.trade_from is None else str(cfg.trade_from)
    return BacktestResult(
        trades=trades_df, equity=equity_df, stats=stats,
        start=start, end=str(exec_bars["ts"].max()),
        exec_timeframe=etf, strategy_timeframe=stf, initial_balance=cfg.initial_balance,
        params=dict(strategy.params),
    )


_TRADE_SCHEMA = {
    "signal_ts": pl.Int64, "entry_ts": pl.Int64, "exit_ts": pl.Int64, "side": pl.Utf8,
    "lots": pl.Float64, "entry_price": pl.Float64, "exit_price": pl.Float64, "sl": pl.Float64,
    "tp": pl.Float64, "exit_reason": pl.Utf8, "entry_kind": pl.Utf8, "tag": pl.Utf8,
    "gross_pnl": pl.Float64, "commission": pl.Float64, "swap": pl.Float64,
    "spread_cost": pl.Float64, "slippage_cost": pl.Float64, "net_pnl": pl.Float64,
    "r_multiple": pl.Float64, "bars_held": pl.Int64,
}


def _trades_frame(trades: list[dict]) -> pl.DataFrame:
    df = pl.DataFrame(trades, schema=_TRADE_SCHEMA)
    return df.with_columns(
        [pl.from_epoch(c, time_unit="ns").dt.replace_time_zone("UTC").alias(c)
         for c in ("signal_ts", "entry_ts", "exit_ts")]
    )
