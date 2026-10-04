"""High-level history jobs: bars, ticks and spread calibration over a date range."""

from datetime import datetime

import polars as pl
from twisted.internet import defer

from ctlab.broker.client import Session
from ctlab.broker.decode import (
    WINDOW_MS,
    bars_frame,
    page_bars_backward,
    page_ticks_backward,
    ticks_frame,
    windows,
)

DAY_MS = 86_400_000


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


async def fetch_bars(s: Session, symbol_id: int, digits: int, period: str,
                     start: datetime, end: datetime, progress=None) -> pl.DataFrame:
    wins = windows(to_ms(start), to_ms(end), WINDOW_MS[period])
    done = 0

    async def one(w):
        nonlocal done
        rows = await page_bars_backward(
            lambda f, t: s.trendbars(symbol_id, period, f, t), w[0], w[1])
        done += 1
        if progress:
            progress(done, len(wins))
        return rows

    results = await defer.gatherResults([defer.ensureDeferred(one(w)) for w in wins],
                                        consumeErrors=True)
    return bars_frame([r for rows in results for r in rows], digits)


async def fetch_quotes_day(s: Session, symbol_id: int, digits: int,
                           day_start_ms: int, day_end_ms: int) -> pl.DataFrame:
    bid, ask = await defer.gatherResults([
        defer.ensureDeferred(page_ticks_backward(
            lambda f, t: s.ticks(symbol_id, "BID", f, t), day_start_ms, day_end_ms)),
        defer.ensureDeferred(page_ticks_backward(
            lambda f, t: s.ticks(symbol_id, "ASK", f, t), day_start_ms, day_end_ms)),
    ], consumeErrors=True)
    return ticks_frame(bid, ask, digits)


def day_windows(start: datetime, end: datetime) -> list[tuple[int, int]]:
    return windows(to_ms(start), to_ms(end), DAY_MS)
