"""Parquet storage: data/parquet/<SYMBOL>/<TF>/<YYYY>.parquet (ticks: TICK/<YYYY-MM>.parquet)."""

import os
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from ctlab.config import env
from ctlab.data.schema import BAR_SCHEMA, TICK_SCHEMA, conform

TICK = "TICK"


def dataset_dir(symbol: str, timeframe: str, root: Path | None = None) -> Path:
    root = root or env().ctlab_data_dir / "parquet"
    return root / symbol.upper() / timeframe.upper()


def _schema(timeframe: str) -> dict:
    return TICK_SCHEMA if timeframe.upper() == TICK else BAR_SCHEMA


def _partition_key(timeframe: str) -> pl.Expr:
    fmt = "%Y-%m" if timeframe.upper() == TICK else "%Y"
    return pl.col("ts").dt.strftime(fmt).alias("_part")


def _atomic_write(df: pl.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(".parquet.tmp")
    df.write_parquet(tmp, compression="zstd", statistics=True)
    os.replace(tmp, path)


def write(df: pl.DataFrame, symbol: str, timeframe: str, root: Path | None = None) -> int:
    """Merge rows into storage. On equal `ts` the new row wins. Returns rows written."""
    schema = _schema(timeframe)
    df = conform(df, schema)
    d = dataset_dir(symbol, timeframe, root)
    d.mkdir(parents=True, exist_ok=True)
    for (part,), chunk in df.with_columns(_partition_key(timeframe)).group_by("_part"):
        path = d / f"{part}.parquet"
        chunk = chunk.drop("_part")
        if path.exists():
            chunk = pl.concat([pl.read_parquet(path), chunk])
        chunk = chunk.unique(subset="ts", keep="last").sort("ts")
        _atomic_write(chunk, path)
    return df.height


def scan(symbol: str, timeframe: str, start: datetime | None = None,
         end: datetime | None = None, root: Path | None = None) -> pl.LazyFrame:
    """Lazy scan of stored data, filtered to [start, end)."""
    d = dataset_dir(symbol, timeframe, root)
    files = sorted(d.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"No data for {symbol} {timeframe} in {d}")
    lf = pl.scan_parquet(files)
    if start is not None:
        lf = lf.filter(pl.col("ts") >= _utc(start))
    if end is not None:
        lf = lf.filter(pl.col("ts") < _utc(end))
    return lf.sort("ts")


def read(symbol: str, timeframe: str, start: datetime | None = None,
         end: datetime | None = None, root: Path | None = None) -> pl.DataFrame:
    return scan(symbol, timeframe, start, end, root).collect()


def list_datasets(root: Path | None = None) -> list[dict]:
    root = root or env().ctlab_data_dir / "parquet"
    out = []
    for d in sorted(p for p in root.glob("*/*") if p.is_dir()):
        files = sorted(d.glob("*.parquet"))
        if not files:
            continue
        stats = pl.scan_parquet(files).select(
            pl.len().alias("rows"), pl.col("ts").min().alias("first"), pl.col("ts").max().alias("last")
        ).collect().row(0, named=True)
        out.append({"symbol": d.parent.name, "timeframe": d.name, **stats})
    return out


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
