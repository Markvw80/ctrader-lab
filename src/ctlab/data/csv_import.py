"""CSV import driven by a small format mapping (config/csv_formats/<name>.yaml).

Mapping keys:
  kind:            bars | ticks
  separator:       field separator ("\\t" for tab)
  has_header:      bool
  columns:         canonical name -> source column (name, or 0-based index if no header).
                   Timestamp is either `datetime`, or `date` + `time`.
  datetime_format: strftime format, or "epoch_ms" / "epoch_s"
  timezone:        IANA name, or "NY+7" style broker server clock
"""

from pathlib import Path

import polars as pl
import yaml

from ctlab.config import env
from ctlab.data.schema import BAR_SCHEMA, TICK_SCHEMA, conform
from ctlab.data.timezones import localize_to_utc


def load_format(name_or_path: str) -> dict:
    p = Path(name_or_path)
    if not p.suffix:
        p = env().ctlab_config_dir / "csv_formats" / f"{name_or_path}.yaml"
    with p.open() as f:
        fmt = yaml.safe_load(f)
    fmt["separator"] = fmt.get("separator", ",").encode().decode("unicode_escape")
    return fmt


def _source(col, has_header: bool) -> pl.Expr:
    return pl.col(col if has_header else f"column_{int(col) + 1}")


def _parse_ts(fmt: dict) -> pl.Expr:
    cols, has_header = fmt["columns"], fmt.get("has_header", True)
    dt_fmt = fmt["datetime_format"]
    if "datetime" in cols:
        raw = _source(cols["datetime"], has_header).str.strip_chars()
    else:
        raw = pl.concat_str(
            [_source(cols["date"], has_header).str.strip_chars(),
             _source(cols["time"], has_header).str.strip_chars()],
            separator=" ",
        )
    if dt_fmt in ("epoch_ms", "epoch_s"):
        unit = "ms" if dt_fmt == "epoch_ms" else "s"
        ts = pl.from_epoch(raw.cast(pl.Int64), time_unit=unit)
    else:
        ts = raw.str.strptime(pl.Datetime("us"), dt_fmt, strict=True)
    return localize_to_utc(ts, fmt.get("timezone", "UTC")).alias("ts")


def read_csv(path: str | Path, fmt: dict) -> pl.DataFrame:
    """Read a CSV into the canonical bar/tick schema (UTC). No cleaning yet."""
    has_header = fmt.get("has_header", True)
    df = pl.read_csv(path, separator=fmt["separator"], has_header=has_header,
                     infer_schema=False, truncate_ragged_lines=True)
    cols = fmt["columns"]
    schema = BAR_SCHEMA if fmt["kind"] == "bars" else TICK_SCHEMA
    exprs = [_parse_ts(fmt)]
    for name, dtype in schema.items():
        if name == "ts":
            continue
        if name in cols:
            e = _source(cols[name], has_header).str.strip_chars().cast(pl.Float64)
            exprs.append((e.round(0) if dtype == pl.Int64 else e).cast(dtype).alias(name))
        elif name == "tick_volume":
            exprs.append(pl.lit(0, dtype=pl.Int64).alias(name))
        else:
            raise ValueError(f"Format has no mapping for required column {name!r}")
    return conform(df.select(exprs), schema)


def clean(df: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """Sort and drop exact duplicate rows. Conflicting duplicates are left for validation."""
    n = df.height
    out = df.unique(maintain_order=False).sort("ts")
    return out, {"rows_read": n, "exact_duplicates_dropped": n - out.height}
