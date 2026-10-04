"""Pure decoding and paging logic for cTrader Open API history (no network, unit-tested).

Prices on the wire are integers in 1/100000 of the quote unit, independent of `digits`.
"""

from collections.abc import Awaitable, Callable, Iterable

import polars as pl

from ctlab.data.schema import BAR_SCHEMA, TICK_SCHEMA

PRICE_SCALE = 100_000

PERIOD_MS = {
    "M1": 60_000, "M5": 300_000, "M15": 900_000, "M30": 1_800_000,
    "H1": 3_600_000, "H4": 14_400_000, "D1": 86_400_000,
}
# Request window per period; deliberately small so a single response never needs truncation.
WINDOW_MS = {
    "M1": 5 * 86_400_000, "M5": 20 * 86_400_000, "M15": 60 * 86_400_000,
    "M30": 120 * 86_400_000, "H1": 200 * 86_400_000, "H4": 365 * 86_400_000,
    "D1": 5 * 365 * 86_400_000,
}

Bar = tuple[int, float, float, float, float, int]  # ts_ms, open, high, low, close, volume
Tick = tuple[int, float]                            # ts_ms, price


def decode_trendbar(tb) -> Bar:
    """ProtoOATrendbar -> (ts_ms, open, high, low, close, tick_volume)."""
    low = tb.low
    return (
        tb.utcTimestampInMinutes * 60_000,
        (low + tb.deltaOpen) / PRICE_SCALE,
        (low + tb.deltaHigh) / PRICE_SCALE,
        low / PRICE_SCALE,
        (low + tb.deltaClose) / PRICE_SCALE,
        int(tb.volume),
    )


def decode_ticks(tick_data: Iterable) -> list[Tick]:
    """ProtoOATickData list: first entry absolute, the rest deltas to the previous entry."""
    out: list[Tick] = []
    ts = price = 0
    for i, t in enumerate(tick_data):
        ts = t.timestamp if i == 0 else ts + t.timestamp
        price = t.tick if i == 0 else price + t.tick
        out.append((ts, price / PRICE_SCALE))
    return out


def windows(start_ms: int, end_ms: int, window_ms: int) -> list[tuple[int, int]]:
    """Split [start, end) into consecutive windows, newest last."""
    out, s = [], start_ms
    while s < end_ms:
        e = min(s + window_ms, end_ms)
        out.append((s, e))
        s = e
    return out


async def page_bars_backward(fetch: Callable[[int, int], Awaitable[list[Bar]]],
                             start_ms: int, end_ms: int) -> list[Bar]:
    """Fetch all bars in [start, end), paging backwards from `end` in case a response is capped.

    The server returns the bars closest to `toTimestamp` when it limits the count, so asking
    again with `to = oldest received` fills the remainder. Duplicates are removed by the caller.
    """
    rows: list[Bar] = []
    to, prev_oldest = end_ms, None
    while to > start_ms:
        chunk = [b for b in await fetch(start_ms, to) if start_ms <= b[0] < end_ms]
        if not chunk:
            break
        rows.extend(chunk)
        oldest = min(b[0] for b in chunk)
        if oldest <= start_ms or oldest == prev_oldest:
            break
        prev_oldest, to = oldest, oldest
    return rows


async def page_ticks_backward(fetch: Callable[[int, int], Awaitable[tuple[list[Tick], bool]]],
                              start_ms: int, end_ms: int) -> list[Tick]:
    """Fetch all ticks in [start, end) using `hasMore` paging (newest first)."""
    out: list[Tick] = []
    to = end_ms
    while True:
        ticks, has_more = await fetch(start_ms, to)
        out.extend(t for t in ticks if start_ms <= t[0] < end_ms)
        if not has_more or not ticks:
            return out
        oldest = min(t[0] for t in ticks)
        to = oldest if oldest < to else to - 1


def bars_frame(rows: list[Bar], digits: int) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=["ts_ms", "open", "high", "low", "close", "tick_volume"],
                      orient="row")
    if df.height == 0:
        return pl.DataFrame(schema=BAR_SCHEMA)
    return (
        df.unique(subset="ts_ms", keep="last")
        .with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.replace_time_zone("UTC").alias("ts"))
        .with_columns([pl.col(c).round(digits) for c in ("open", "high", "low", "close")])
        .select(list(BAR_SCHEMA))
        .cast(BAR_SCHEMA)
        .sort("ts")
    )


def ticks_frame(bid: list[Tick], ask: list[Tick], digits: int) -> pl.DataFrame:
    """Merge separate bid and ask streams into quotes; each side is forward-filled."""
    if not bid or not ask:
        return pl.DataFrame(schema=TICK_SCHEMA)
    b = pl.DataFrame(bid, schema=["ts_ms", "bid"], orient="row").unique(maintain_order=True)
    a = pl.DataFrame(ask, schema=["ts_ms", "ask"], orient="row").unique(maintain_order=True)
    b = b.group_by("ts_ms").agg(pl.col("bid").last())
    a = a.group_by("ts_ms").agg(pl.col("ask").last())
    df = (
        b.join(a, on="ts_ms", how="full", coalesce=True)
        .sort("ts_ms")
        .with_columns(pl.col("bid").forward_fill(), pl.col("ask").forward_fill())
        .drop_nulls()
        .with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.replace_time_zone("UTC").alias("ts"))
        .with_columns(pl.col("bid").round(digits), pl.col("ask").round(digits))
    )
    return df.select(list(TICK_SCHEMA)).cast(TICK_SCHEMA)
