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


@app.command()
def backtest(strategy: str, symbol: str = "XAUUSD", timeframe: str = "M1") -> None:
    """Run a single backtest."""
    _todo(3)


@app.command()
def optimize(strategy: str, trials: int = 100) -> None:
    """Optuna optimization on the in-sample part (holdout excluded)."""
    _todo(4)


@app.command()
def walkforward(strategy: str) -> None:
    """Rolling walk-forward: optimize IS, validate OOS, report robustness."""
    _todo(4)


@app.command()
def report(run_id: str) -> None:
    """(Re)generate the HTML report for a run."""
    _todo(5)


if __name__ == "__main__":
    app()
