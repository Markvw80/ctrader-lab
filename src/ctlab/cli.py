"""ctlab command line. Run `ctlab --help`."""

import typer

from ctlab import __version__
from ctlab.config import env, settings, symbol_spec

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
def data_import_csv(path: str, symbol: str = "XAUUSD", timeframe: str = "M1") -> None:
    """Import a CSV file into Parquet storage."""
    _todo(2)


@data_app.command("fetch")
def data_fetch(symbol: str = "XAUUSD", timeframe: str = "M1",
               start: str = typer.Option(...), end: str = typer.Option(...)) -> None:
    """Fetch history from the cTrader Open API."""
    _todo(6)


@data_app.command("validate")
def data_validate(symbol: str = "XAUUSD", timeframe: str = "M1") -> None:
    """Check stored data for gaps, duplicates and timezone issues."""
    _todo(2)


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
