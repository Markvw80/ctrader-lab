"""ctlab command line. Run `ctlab --help`."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import typer

from ctlab import __version__
from ctlab.broker.run import run
from ctlab.broker.spec import (
    load_overrides,
    merge_profiles,
    save_overrides,
    spread_profile,
    symbol_overrides,
)
from ctlab.config import env, save_tokens, settings, symbol_spec
from ctlab.data import store
from ctlab.data.csv_import import clean, load_format, read_csv
from ctlab.data.market_hours import MarketHours
from ctlab.data.schema import timeframe_minutes
from ctlab.data.validate import Report, validate_bars, validate_ticks

app = typer.Typer(no_args_is_help=True, help="cTrader research lab (research only, no live orders).")
data_app = typer.Typer(no_args_is_help=True, help="Fetch, import and validate market data.")
app.add_typer(data_app, name="data")
api_app = typer.Typer(no_args_is_help=True, help="cTrader Open API: accounts, symbol spec, tokens.")
app.add_typer(api_app, name="api")


def _todo(phase: int) -> None:
    typer.echo(f"Not implemented yet (phase {phase}).")
    raise typer.Exit(1)


@app.command()
def info() -> None:
    """Show version, paths and loaded config."""
    e = env()
    typer.echo(f"ctlab {__version__}  commit={e.ctlab_git_commit}  env={e.ctrader_env}")
    typer.echo(f"data={e.ctlab_data_dir}  results={e.ctlab_results_dir}  config={e.ctlab_config_dir}")
    typer.echo(f"account={settings()['account']}")
    typer.echo(f"XAUUSD contract_size={symbol_spec('XAUUSD')['contract_size']}")


@data_app.command("import-csv")
def data_import_csv(
    path: Path,
    fmt: str = typer.Option("generic", "--format", help="Name in config/csv_formats or a .yaml path"),
    symbol: str = "XAUUSD",
    timeframe: str = typer.Option("M1", help="Ignored for tick formats"),
    force: bool = typer.Option(False, help="Write even if validation reports errors"),
) -> None:
    """Import a CSV file into Parquet storage (UTC), with validation."""
    f = load_format(fmt)
    df, stats = clean(read_csv(path, f))
    tf = store.TICK if f["kind"] == "ticks" else timeframe.upper()
    typer.echo(f"read {stats['rows_read']} rows, dropped {stats['exact_duplicates_dropped']} exact duplicates")
    if not _ingest(df, symbol, tf, force):
        raise typer.Exit(1)


@data_app.command("fetch")
def data_fetch(
    symbol: str = "XAUUSD",
    timeframe: str = "M1",
    start: str | None = typer.Option(None, help="UTC date/time; default: continue after last stored bar"),
    end: str | None = typer.Option(None, help="UTC date/time (exclusive); default: now"),
    force: bool = typer.Option(False, help="Write even if validation reports errors"),
) -> None:
    """Fetch bid trendbars from the cTrader Open API (also refreshes broker symbol spec)."""
    tf = timeframe.upper()
    if start is None:
        try:
            last = store.scan(symbol, tf).select(pl.col("ts").max()).collect().item()
        except FileNotFoundError:
            typer.echo("No stored data yet: pass --start.")
            raise typer.Exit(1) from None
        s = last + timedelta(minutes=timeframe_minutes(tf))
    else:
        s = _parse_utc(start)
    e = _parse_utc(end) if end else datetime.now(UTC).replace(second=0, microsecond=0)
    typer.echo(f"fetching {symbol} {tf} {s} -> {e}")

    from ctlab.broker.history import fetch_bars

    async def job():
        async with _session() as sess:
            sym = await _sync_symbol(sess, symbol)
            df = await fetch_bars(sess, sym.symbolId, sym.digits, tf, s, e,
                                  progress=lambda d, n: typer.echo(f"  window {d}/{n}") if d % 25 == 0 or d == n else None)
        typer.echo(f"received {df.height} bars")
        if df.height and not _ingest(df, symbol, tf, force):
            raise SystemExit(1)

    run(job)


@data_app.command("fetch-ticks")
def data_fetch_ticks(symbol: str = "XAUUSD", start: str = typer.Option(...),
                     end: str = typer.Option(...)) -> None:
    """Fetch bid+ask ticks day by day and store them (large: use short ranges)."""
    from ctlab.broker.history import day_windows, fetch_quotes_day

    s, e = _parse_utc(start), _parse_utc(end)

    async def job():
        async with _session() as sess:
            sym = await _sync_symbol(sess, symbol)
            for f, t in day_windows(s, e):
                q = await fetch_quotes_day(sess, sym.symbolId, sym.digits, f, t)
                if q.height:
                    store.write(q, symbol, store.TICK)
                typer.echo(f"  {datetime.fromtimestamp(f / 1000, UTC):%Y-%m-%d}: {q.height} quotes")

    run(job)


@data_app.command("calibrate-spread")
def data_calibrate_spread(
    symbol: str = "XAUUSD",
    days: int = typer.Option(20, help="Number of recent calendar days to sample"),
    store_ticks: bool = typer.Option(False, help="Also store the fetched ticks"),
) -> None:
    """Measure the real spread per New York hour from bid/ask ticks; saved as broker override."""
    e = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    s = e - timedelta(days=days)
    from ctlab.broker.history import day_windows, fetch_quotes_day

    async def job():
        profiles = []
        async with _session() as sess:
            sym = await _sync_symbol(sess, symbol)
            for f, t in day_windows(s, e):
                q = await fetch_quotes_day(sess, sym.symbolId, sym.digits, f, t)
                if q.height == 0:
                    continue
                if store_ticks:
                    store.write(q, symbol, store.TICK)
                profiles.append(spread_profile(q))
                typer.echo(f"  {datetime.fromtimestamp(f / 1000, UTC):%Y-%m-%d}: {q.height} quotes, "
                           f"median spread {profiles[-1]['overall_median']}")
        if not profiles:
            typer.echo("No ticks received.")
            raise SystemExit(1)
        prof = merge_profiles(profiles)
        path = save_overrides(symbol, {"costs": {"spread": prof}})
        typer.echo(f"spread per NY hour (median / p90), saved to {path}:")
        for h, v in prof["hours"].items():
            typer.echo(f"  {h:02d}:00  {v['median']:.3f} / {v['p90']:.3f}")

    run(job)


@data_app.command("validate")
def data_validate(symbol: str = "XAUUSD", timeframe: str = "M1") -> None:
    """Check stored data for gaps, duplicates and timezone issues."""
    tf = timeframe.upper()
    rep = _validate(store.read(symbol, tf), symbol, tf)
    _print_report(rep)
    _save_report(rep, symbol, tf)
    if not rep.ok:
        raise typer.Exit(1)


@data_app.command("list")
def data_list() -> None:
    """List stored datasets."""
    for d in store.list_datasets():
        typer.echo(f"{d['symbol']:<8} {d['timeframe']:<5} {d['rows']:>10} rows  {d['first']}  ->  {d['last']}")


def _ingest(df: pl.DataFrame, symbol: str, tf: str, force: bool) -> bool:
    rep = _validate(df, symbol, tf)
    _print_report(rep)
    if not rep.ok and not force:
        typer.echo("Validation errors: nothing written. Fix the source/format or use --force.")
        return False
    df = df.filter(pl.col("ts").is_not_null())
    n = store.write(df, symbol, tf)
    _save_report(rep, symbol, tf)
    typer.echo(f"wrote {n} rows to {store.dataset_dir(symbol, tf)}")
    return True


def _parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@asynccontextmanager
async def _session():
    from ctlab.broker.client import ApiError, Session

    sess = Session(env())
    try:
        await sess.open()
        yield sess
    except ApiError as ex:
        typer.echo(f"API error {ex}")
        if "TOKEN" in ex.code.upper():
            typer.echo("Access token invalid/expired? Run `ctlab api refresh-token`.")
        raise SystemExit(1) from None
    finally:
        sess.close()


async def _sync_symbol(sess, symbol: str):
    sym = await sess.symbol(symbol)
    save_overrides(symbol, symbol_overrides(sym))
    return sym


def _validate(df: pl.DataFrame, symbol: str, tf: str) -> Report:
    hours = MarketHours.from_spec(symbol_spec(symbol))
    return validate_ticks(df, hours) if tf == store.TICK else validate_bars(df, tf, hours)


def _print_report(rep: Report) -> None:
    typer.echo(f"{rep.kind} {rep.timeframe}: {rep.rows} rows  {rep.first} -> {rep.last}")
    if rep.gaps:
        g = rep.gaps
        typer.echo(f"gaps: {g.get('gaps', 0)}  missing bars: {g.get('missing_bars', 0)}"
                   f" ({g.get('missing_pct', 0)}% of expected)  buckets: {g.get('buckets', {})}")
    for i in rep.issues:
        typer.echo(f"  [{i.level.upper():7}] {i.code}: {i.message} (n={i.count})")
        for s in i.samples:
            typer.echo(f"             {s}")
    typer.echo("OK" if rep.ok else "ERRORS FOUND")


def _save_report(rep: Report, symbol: str, tf: str) -> None:
    d = store.dataset_dir(symbol, tf)
    d.mkdir(parents=True, exist_ok=True)
    (d / "_validation.json").write_text(json.dumps(rep.to_dict(), indent=2, default=str))


@api_app.command("check")
def api_check() -> None:
    """Step-by-step connection test: app credentials, access token, account."""
    from ctlab.broker.client import ApiError, Session
    from ctlab.config import tokens

    async def job():
        sess = Session(env())
        try:
            await sess.connect(live=False)
            typer.echo("1. app credentials (client id/secret): OK")
            if len(tokens()[0]) < 10:
                typer.echo("2. access token: MISSING -> generate one (see README) and set CTRADER_ACCESS_TOKEN")
                raise SystemExit(1)
            accounts = await sess.accounts()
            typer.echo(f"2. access token: OK ({len(accounts)} account(s))")
            for a in accounts:
                typer.echo(f"     {a.ctidTraderAccountId:>12}  {'LIVE' if a.isLive else 'demo'}  login {a.traderLogin}")
            if not env().ctrader_account_id:
                typer.echo("3. CTRADER_ACCOUNT_ID: not set -> pick one of the ids above")
                raise SystemExit(1)
        except ApiError as ex:
            typer.echo(f"   FAILED: {ex}")
            raise SystemExit(1) from None
        finally:
            sess.close()
        async with _session() as s2:
            sym = await s2.symbol("XAUUSD")
            typer.echo(f"3. account {env().ctrader_account_id}: OK, XAUUSD symbolId {sym.symbolId}")

    run(job)


@api_app.command("accounts")
def api_accounts() -> None:
    """List trading accounts available to the access token."""
    from ctlab.broker.client import Session

    async def job():
        sess = Session(env())
        try:
            await sess.connect(live=False)
            for a in await sess.accounts():
                kind = "LIVE" if a.isLive else "demo"
                typer.echo(f"{a.ctidTraderAccountId:>12}  {kind:<4}  login {a.traderLogin}")
        finally:
            sess.close()

    run(job)


@api_app.command("symbol")
def api_symbol(symbol: str = "XAUUSD") -> None:
    """Fetch the broker's symbol spec (contract, commission, swap) and save it as override."""

    async def job():
        async with _session() as sess:
            await _sync_symbol(sess, symbol)
        o = load_overrides(symbol)
        typer.echo(json.dumps(o, indent=2))

    run(job)


@api_app.command("refresh-token")
def api_refresh_token() -> None:
    """Get a new access token with the refresh token; stored in data/broker/.tokens.json (0600)."""
    from ctlab.broker.client import Session

    async def job():
        sess = Session(env())
        try:
            await sess.connect(live=False)
            t = await sess.refresh_token()
        finally:
            sess.close()
        p = save_tokens(t["access_token"], t["refresh_token"], t["expires_in"])
        typer.echo(f"new token saved to {p}, valid for {t['expires_in'] // 86400} days")

    run(job)


def _parse_params(strategy_cls, items: list[str]) -> dict:
    specs = strategy_cls.param_specs()
    out = {}
    for item in items:
        if "=" not in item:
            raise typer.BadParameter(f"--param expects name=value, got {item!r}")
        k, v = item.split("=", 1)
        if k not in specs:
            raise typer.BadParameter(f"unknown parameter {k!r}; available: {sorted(specs)}")
        out[k] = specs[k].parse(v)
    return out


@app.command()
def strategies() -> None:
    """List available strategies and their parameters."""
    from ctlab.strategy.base import all_strategies

    for name, cls in sorted(all_strategies().items()):
        typer.echo(f"{name}  (timeframe {cls.timeframe})")
        for k, p in cls.param_specs().items():
            rng = f"{p.choices}" if p.kind == "choice" else f"[{p.low}, {p.high}]"
            typer.echo(f"    {k:<20} {p.kind:<6} default={p.default!s:<8} range={rng}  {p.doc}")


@app.command()
def backtest(
    strategy: str,
    symbol: str = "XAUUSD",
    start: str | None = typer.Option(None, help="UTC, e.g. 2024-01-01"),
    end: str | None = typer.Option(None, help="UTC, exclusive"),
    param: list[str] = typer.Option([], "--param", "-p", help="name=value, repeatable"),
    exec_tf: str | None = typer.Option(None, help="Execution timeframe (default from settings)"),
    spread_stat: str | None = typer.Option(None, help="median | p90"),
    balance: float | None = None,
    risk: float | None = typer.Option(None, help="Risk per trade in %"),
    daily_limit: float | None = typer.Option(None, help="Daily loss limit in %"),
    save: bool = typer.Option(True, help="Store the run under results/runs"),
) -> None:
    """Run a single backtest and print the metrics."""
    from ctlab.engine.runner import backtest as run_bt
    from ctlab.engine.runner import load_inputs
    from ctlab.report.metrics import breakdowns, compute_metrics
    from ctlab.runs.registry import data_fingerprint, new_run_id, save_run
    from ctlab.strategy.base import get_strategy

    cls = get_strategy(strategy)
    params = _parse_params(cls, param)
    strat = cls(**params)
    inp = load_inputs(symbol, _parse_utc(start) if start else None, _parse_utc(end) if end else None,
                      exec_tf, spread_stat,
                      {"initial_balance": balance, "risk_per_trade_pct": risk,
                       "daily_loss_limit_pct": daily_limit})
    res = run_bt(strat, inp)
    m = compute_metrics(res, inp.sessions)
    b = breakdowns(res, inp.sessions)
    _print_metrics(m, b)
    if save:
        rid = new_run_id(strategy)
        save_run(rid, "backtest", {
            "strategy": strategy, "params": strat.params, "symbol": symbol,
            "strategy_timeframe": res.strategy_timeframe, "exec_timeframe": res.exec_timeframe,
            "requested_range": [start, end], "data": data_fingerprint(inp.bars),
            "engine": inp.cfg, "cost_model": inp.cost, "sessions": inp.sessions, "spread_stat": inp.spread_stat,
        }, m, {"trades": res.trades, "equity": res.equity, **{f"breakdown_{k}": v for k, v in b.items()}})
        _finish(rid)


@app.command()
def check(
    strategy: str,
    symbol: str = "XAUUSD",
    start: str | None = None,
    end: str | None = None,
    param: list[str] = typer.Option([], "--param", "-p"),
) -> None:
    """Look-ahead check: indicators and trades must not depend on future data."""
    from ctlab.engine.runner import load_inputs
    from ctlab.strategy.base import get_strategy
    from ctlab.strategy.causality import check_strategy

    cls = get_strategy(strategy)
    inp = load_inputs(symbol, _parse_utc(start) if start else None, _parse_utc(end) if end else None)
    rep = check_strategy(cls, _parse_params(cls, param), inp.bars, inp.cost, inp.sessions)
    typer.echo(f"{rep.checks} checks: {'OK' if rep.ok else 'LOOK-AHEAD DETECTED'}")
    for p in rep.problems:
        typer.echo(f"  - {p}")
    if not rep.ok:
        raise typer.Exit(1)


def _print_metrics(m: dict, b: dict) -> None:
    keys = ["trades", "net_profit", "return_pct", "profit_factor", "win_rate_pct", "avg_trade",
            "avg_r", "max_drawdown", "max_drawdown_pct", "sharpe", "long_trades", "short_trades"]
    for k in keys:
        typer.echo(f"  {k:<18} {m[k]}")
    typer.echo(f"  {'costs':<18} {m['costs']}")
    typer.echo(f"  {'exit_reasons':<18} {m['exit_reasons']}")
    typer.echo(f"  {'engine':<18} {m['engine']}")
    for name, df in b.items():
        typer.echo(f"\nper {name}:")
        typer.echo(str(df))


def _opt_settings(objective: str | None) -> dict:
    s = dict(settings()["optimization"])
    if objective:
        s["objective"] = objective
    return s


def _research_split(symbol: str, s: dict, end: str | None):
    """Fixed holdout boundary (from all stored data) and the research end for this run."""
    from ctlab.optimize.splits import holdout_start

    lf = store.scan(symbol, "M1").select(pl.col("ts").min().alias("a"), pl.col("ts").max().alias("b"))
    first, last = lf.collect().row(0)
    h = holdout_start(symbol, first, last, s["holdout_pct"])
    research_end = min(h, _parse_utc(end)) if end else h
    return h, research_end


@app.command()
def optimize(
    strategy: str,
    symbol: str = "XAUUSD",
    trials: int = typer.Option(100, help="Optuna trials"),
    start: str | None = typer.Option(None, help="UTC; default: first stored bar"),
    end: str | None = typer.Option(None, help="UTC; capped at the holdout start"),
    objective: str | None = typer.Option(None, help="sharpe | return_dd | net_profit | profit_factor"),
) -> None:
    """Optimize on the whole research period (holdout excluded) + sensitivity analysis.

    This alone says nothing about robustness: use `walkforward` for that.
    """
    from ctlab.engine.runner import load_inputs
    from ctlab.optimize.optuna_runner import Evaluator, best_params, best_valid, trials_frame
    from ctlab.optimize.optuna_runner import optimize as run_opt
    from ctlab.optimize.robustness import sensitivity
    from ctlab.report.metrics import compute_metrics
    from ctlab.runs.registry import data_fingerprint, new_run_id, runs_dir, save_run
    from ctlab.strategy.base import get_strategy

    cls = get_strategy(strategy)
    s = _opt_settings(objective)
    hstart, rend = _research_split(symbol, s, end)
    inp = load_inputs(symbol, _parse_utc(start) if start else None, rend)
    typer.echo(f"research {inp.bars['ts'].min()} -> {rend} (holdout from {hstart} is never used)")
    rid = new_run_id(f"opt-{strategy}")
    rdir = runs_dir() / rid
    rdir.mkdir(parents=True)
    ev = Evaluator(cls, inp, None, None, s["warmup_days"], s["objective"], s["min_trades_per_window"])
    study = run_opt(ev, trials, s["seed"], rdir / "optuna.db", "optimize",
                    progress=lambda d, n: typer.echo(f"  trial {d}/{n}") if d % 20 == 0 else None)
    best = best_valid(study)
    if best is None:
        typer.echo(f"No valid parameter set (every trial < {s['min_trades_per_window']} trades).")
        raise typer.Exit(1)
    params = best_params(best)
    typer.echo(f"best {s['objective']} = {best.value:.3f}  params {params}")
    sens = sensitivity(cls, params, ev.score, s["sensitivity_min_stability"])
    res = ev.run(params)
    m = compute_metrics(res, inp.sessions)
    _print_metrics(m, {})
    _print_sensitivity(sens)
    typer.echo("NOTE: in-sample result only. Run `ctlab walkforward` to judge robustness.")
    save_run(rid, "optimize", {
        "strategy": strategy, "symbol": symbol, "objective": s["objective"], "trials": trials,
        "seed": s["seed"], "holdout_start": str(hstart),
        "research_range": [str(inp.bars["ts"].min()), str(rend)],
        "data": data_fingerprint(inp.bars), "engine": inp.cfg, "cost_model": inp.cost, "sessions": inp.sessions,
        "optimization_settings": s, "best_params": params,
    }, {**m, "best_objective": best.value, "best_params": params,
        "sensitivity": {k: v for k, v in sens.items() if k != "rows"}},
        {"trials": trials_frame(study), "sensitivity": pl.DataFrame(sens["rows"]),
         "trades": res.trades, "equity": res.equity})
    _finish(rid)


@app.command()
def walkforward(
    strategy: str,
    symbol: str = "XAUUSD",
    trials: int = typer.Option(50, help="Optuna trials per window"),
    start: str | None = None,
    end: str | None = typer.Option(None, help="UTC; capped at the holdout start"),
    objective: str | None = None,
    is_months: int | None = None,
    oos_months: int | None = None,
    anchored: bool | None = None,
) -> None:
    """Rolling walk-forward on the research period; ends with ROBUST / NOT ROBUST."""
    from ctlab.engine.runner import load_inputs
    from ctlab.optimize.walkforward import run_walkforward
    from ctlab.runs.registry import data_fingerprint, new_run_id, runs_dir, save_run
    from ctlab.strategy.base import get_strategy

    cls = get_strategy(strategy)
    s = _opt_settings(objective)
    for k, v in (("wf_in_sample_months", is_months), ("wf_out_of_sample_months", oos_months),
                 ("wf_anchored", anchored)):
        if v is not None:
            s[k] = v
    hstart, rend = _research_split(symbol, s, end)
    inp = load_inputs(symbol, _parse_utc(start) if start else None, rend)
    typer.echo(f"research {inp.bars['ts'].min()} -> {rend} (holdout from {hstart} is never used)")
    rid = new_run_id(f"wf-{strategy}")
    rdir = runs_dir() / rid
    rdir.mkdir(parents=True)
    wf = run_walkforward(cls, inp, rend, s, trials, rdir / "optuna.db", log=typer.echo)

    typer.echo("\nwindow  IS trades     IS net   OOS trades    OOS net  OOS sharpe")
    for w in wf.windows:
        typer.echo(f"{w['window']:>6}  {w['is_trades'] or 0:>9}  {w['is_net'] or 0:>9.2f}"
                   f"  {w['oos_trades']:>10}  {w['oos_net']:>9.2f}  {w['oos_sharpe']}")
    typer.echo(f"\nin-sample   {wf.is_annual_pct:>8.2f} % per year (best trials)")
    typer.echo(f"out-of-sample {wf.oos_annual_pct:>6.2f} % per year  (combined, "
               f"{wf.oos_metrics['trades']} trades, max DD {wf.oos_metrics['max_drawdown_pct']}%, "
               f"sharpe {wf.oos_metrics['sharpe']})")
    if wf.sensitivity:
        _print_sensitivity(wf.sensitivity)
    v = wf.verdict
    typer.echo(f"\nVERDICT: {v['label']}")
    for r in v["reasons"]:
        typer.echo(f"  - {r}")
    windows_df = pl.DataFrame([{**w, "params": json.dumps(w["params"])} for w in wf.windows])
    save_run(rid, "walkforward", {
        "strategy": strategy, "symbol": symbol, "objective": s["objective"],
        "trials_per_window": trials, "seed": s["seed"], "holdout_start": str(hstart),
        "research_range": list(wf.research_range), "data": data_fingerprint(inp.bars),
        "engine": inp.cfg, "cost_model": inp.cost, "sessions": inp.sessions, "optimization_settings": s,
        "final_params": wf.windows[-1]["params"] if wf.windows else None,
    }, {"verdict": v, "is_annual_pct": wf.is_annual_pct, "oos_annual_pct": wf.oos_annual_pct,
        "oos": wf.oos_metrics,
        "sensitivity": None if not wf.sensitivity else
        {k: x for k, x in wf.sensitivity.items() if k != "rows"}},
        {"windows": windows_df, "trades": wf.oos_trades, "equity": wf.oos_equity,
         "trials": wf.trials,
         **({"sensitivity": pl.DataFrame(wf.sensitivity["rows"])} if wf.sensitivity else {})})
    _finish(rid)


@app.command()
def holdout(run_id: str) -> None:
    """Evaluate the parameters of an optimize/walkforward run ONCE on the untouched holdout."""
    from ctlab.engine.runner import load_inputs
    from ctlab.optimize.optuna_runner import Evaluator
    from ctlab.report.metrics import breakdowns, compute_metrics
    from ctlab.runs.registry import data_fingerprint, list_runs, load_run, new_run_id, save_run
    from ctlab.strategy.base import get_strategy

    meta, _, _ = load_run(run_id)
    params = meta.get("best_params") or meta.get("final_params")
    if meta["kind"] not in ("optimize", "walkforward") or not params:
        typer.echo("Run has no parameters to test (need an optimize or walkforward run).")
        raise typer.Exit(1)
    strategy, symbol = meta["strategy"], meta["symbol"]
    previous = [r for r in list_runs() if r["kind"] == "holdout" and r.get("strategy") == strategy]
    if previous:
        typer.echo(f"WARNING: holdout already used {len(previous)}x for {strategy}. Every extra look "
                   "turns the holdout into in-sample data; treat this result as optimistic.")
    s = _opt_settings(None)
    hstart = _parse_utc(meta["holdout_start"])
    inp = load_inputs(symbol, hstart - timedelta(days=s["warmup_days"]), None)
    ev = Evaluator(get_strategy(strategy), inp, hstart, None, s["warmup_days"], s["objective"], 0)
    res = ev.run(params)
    m = compute_metrics(res, inp.sessions)
    b = breakdowns(res, inp.sessions)
    typer.echo(f"holdout {hstart} -> {inp.bars['ts'].max()}  params {params}")
    _print_metrics(m, b)
    rid = new_run_id(f"holdout-{strategy}")
    save_run(rid, "holdout", {
        "strategy": strategy, "symbol": symbol, "source_run": run_id, "params": params,
        "holdout_start": str(hstart), "previous_holdout_runs": len(previous),
        "data": data_fingerprint(inp.bars.filter(pl.col("ts") >= hstart)),
        "engine": inp.cfg, "cost_model": inp.cost, "sessions": inp.sessions,
    }, m, {"trades": res.trades, "equity": res.equity, **{f"breakdown_{k}": v for k, v in b.items()}})
    _finish(rid)


@app.command()
def runs(limit: int = 20) -> None:
    """List stored runs (newest first)."""
    from ctlab.runs.registry import list_runs

    for r in list_runs()[:limit]:
        m = r.get("metrics", {})
        if r["kind"] == "walkforward":
            info = f"{m.get('verdict', {}).get('label')}  OOS {m.get('oos_annual_pct')}%/yr"
        else:
            info = f"net {m.get('net_profit')}  PF {m.get('profit_factor')}  sharpe {m.get('sharpe')}"
        typer.echo(f"{r['run_id']:<48} {r['kind']:<12} {info}")


def _print_sensitivity(sens: dict) -> None:
    typer.echo(f"sensitivity: stability {sens['stability'] and round(sens['stability'], 2)}  "
               f"profitable neighbours {sens['profitable_neighbours_pct']}%  "
               f"{'SENSITIVE' if sens['sensitive'] else 'stable'}")
    for r in sens["reasons"]:
        typer.echo(f"  - {r}")
    if sens["worst"]:
        typer.echo(f"  weakest neighbours: {', '.join(sens['worst'])}")


@app.command()
def report(run_id: str | None = typer.Argument(None, help="Run id; omit with --all")) -> None:
    """(Re)generate the HTML report for a run (or --all runs)."""
    from ctlab.report.html import build_report
    from ctlab.runs.registry import list_runs

    ids = [run_id] if run_id else [r["run_id"] for r in list_runs()]
    for rid in ids:
        typer.echo(f"report: {build_report(rid)}")


def _finish(rid: str) -> None:
    from ctlab.report.html import build_report

    typer.echo(f"run id: {rid}")
    typer.echo(f"report: {build_report(rid)}")


if __name__ == "__main__":
    app()
