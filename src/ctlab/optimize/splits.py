"""Holdout and walk-forward windows.

The holdout start date is fixed on first use (data/research/holdout_<SYMBOL>.json) and never
moves afterwards: newly fetched data lands in the holdout, research data never grows into it.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from ctlab.config import env


@dataclass(frozen=True)
class Window:
    n: int
    is_start: datetime
    is_end: datetime
    oos_start: datetime
    oos_end: datetime


def _holdout_file(symbol: str):
    return env().ctlab_data_dir / "research" / f"holdout_{symbol.upper()}.json"


def holdout_start(symbol: str, first: datetime, last: datetime, pct: float) -> datetime:
    """Fixed holdout boundary; created from `pct` of the available range on first call."""
    p = _holdout_file(symbol)
    if p.exists():
        return datetime.fromisoformat(json.loads(p.read_text())["holdout_start"])
    start = first + (last - first) * (1 - pct / 100)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "holdout_start": start.isoformat(), "pct": pct,
        "data_range_at_fix": [first.isoformat(), last.isoformat()],
        "fixed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": "Never optimize on data >= holdout_start. Delete this file only to start over.",
    }, indent=2))
    return start


def add_months(dt: datetime, months: int) -> datetime:
    y, m = divmod(dt.month - 1 + months, 12)
    return dt.replace(year=dt.year + y, month=m + 1, day=1)


def walkforward_windows(start: datetime, end: datetime, is_months: int, oos_months: int,
                        anchored: bool = False) -> list[Window]:
    """Rolling (or anchored) windows covering [start, end). A last OOS window shorter than
    the full length is kept if it covers at least half of it."""
    base = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if base < start:
        base = add_months(base, 1)
    out, i = [], 0
    while True:
        is_start = base if anchored else add_months(base, i * oos_months)
        is_end = add_months(base, is_months + i * oos_months)
        oos_end = add_months(is_end, oos_months)
        if is_end >= end:
            break
        if oos_end > end:
            if (end - is_end) < (oos_end - is_end) / 2:
                break
            oos_end = end
        out.append(Window(i + 1, is_start, is_end, is_end, oos_end))
        i += 1
    return out
