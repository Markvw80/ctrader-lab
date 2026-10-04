"""ctlab command line. Run `ctlab --help`."""

import json
from pathlib import Path

import polars as pl
import typer

from ctlab import __version__
from ctlab.config import env, settings, symbol_spec
from ctlab.data import store
from ctlab.data.csv_import import clean, load_format, read_csv
from ctlab.data.market_hours import MarketHours
from ctlab.data.validate import Report, validate_bars, validate_ticks

app = typer.Typer(no_args_is_help=True, help="cTrader research lab (research only, no live orders).")
data_app = typer.Typer(no_args_is_help=True, help="Fetch, import and validate market data.")
app.add_typer(data_app, name="data")


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
    rep = _validate(df, symbol, tf)
    typer.echo(f"read {stats['rows_read']} rows, dropped {stats['exact_duplicates_dropped']} exact duplicates")
    _print_report(rep)
    if not rep.ok and not force:
        typer.echo("Validation errors: nothing written. Fix the source/format or use --force.")
        raise typer.Exit(1)
    df = df.filter(pl.col("ts").is_not_null())
    n = store.write(df, symbol, tf)
    _save_report(rep, symbol, tf)
    typer.echo(f"wrote {n} rows to {store.dataset_dir(symbol, tf)}")


@data_app.command("fetch")
def data_fetch(symbol: str = "XAUUSD", timeframe: str = "M1",
               start: str = typer.Option(...), end: str = typer.Option(...)) -> None:
    """Fetch history from the cTrader Open API."""
    _todo(6)


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
