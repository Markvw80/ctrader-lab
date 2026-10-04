"""Data quality checks: duplicates, ordering, alignment, OHLC sanity, gaps, timezone."""

from collections import defaultdict
from dataclasses import asdict, dataclass, field

import polars as pl

from ctlab.data.market_hours import MarketHours
from ctlab.data.schema import TIMEFRAMES, timeframe_minutes

ERROR, WARNING, INFO = "error", "warning", "info"


@dataclass
class Issue:
    level: str
    code: str
    message: str
    count: int = 0
    samples: list = field(default_factory=list)


@dataclass
class Report:
    kind: str
    timeframe: str
    rows: int
    first: str | None
    last: str | None
    issues: list[Issue] = field(default_factory=list)
    gaps: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(i.level == ERROR for i in self.issues)

    def add(self, level, code, message, count=0, samples=None):
        self.issues.append(Issue(level, code, message, count, [str(s) for s in (samples or [])][:5]))

    def to_dict(self) -> dict:
        return {**asdict(self), "ok": self.ok}


def validate_bars(df: pl.DataFrame, timeframe: str, hours: MarketHours) -> Report:
    tf = timeframe.upper()
    tf_min = timeframe_minutes(tf)
    rep = _base_report(df, "bars", tf)
    if df.height == 0:
        rep.add(ERROR, "empty", "No rows")
        return rep
    _common_checks(df, rep)

    misaligned = df.filter(pl.col("ts").dt.truncate(TIMEFRAMES[tf]) != pl.col("ts")) if tf_min <= 60 else df.clear()
    if misaligned.height:
        rep.add(ERROR, "misaligned", f"Timestamps not aligned to {tf} boundaries",
                misaligned.height, misaligned["ts"].head(5).to_list())

    bad_ohlc = df.filter(
        (pl.col("high") < pl.max_horizontal("open", "close"))
        | (pl.col("low") > pl.min_horizontal("open", "close"))
        | (pl.col("high") < pl.col("low"))
    )
    if bad_ohlc.height:
        rep.add(ERROR, "ohlc_inconsistent", "high/low do not contain open/close",
                bad_ohlc.height, bad_ohlc["ts"].head(5).to_list())

    nonpos = df.filter(pl.min_horizontal("open", "high", "low", "close") <= 0)
    if nonpos.height:
        rep.add(ERROR, "nonpositive_price", "Price <= 0", nonpos.height, nonpos["ts"].head(5).to_list())

    if tf_min <= 60:
        spikes = df.filter(
            (pl.col("close") / pl.col("close").shift(1) - 1).abs() * 100 > hours.spike_pct
        )
        if spikes.height:
            rep.add(WARNING, "price_spike", f"Close-to-close move > {hours.spike_pct}% in one bar",
                    spikes.height, spikes["ts"].head(5).to_list())
        _closed_market_check(df, rep, hours)
        _timezone_check(df, rep, hours)
        _gap_check(df, rep, hours, tf, tf_min)
    return rep


def validate_ticks(df: pl.DataFrame, hours: MarketHours) -> Report:
    rep = _base_report(df, "ticks", "TICK")
    if df.height == 0:
        rep.add(ERROR, "empty", "No rows")
        return rep
    _common_checks(df, rep, conflicting_dupes=False)
    crossed = df.filter(pl.col("ask") < pl.col("bid"))
    if crossed.height:
        rep.add(ERROR, "crossed_quotes", "ask < bid", crossed.height, crossed["ts"].head(5).to_list())
    nonpos = df.filter(pl.min_horizontal("bid", "ask") <= 0)
    if nonpos.height:
        rep.add(ERROR, "nonpositive_price", "Price <= 0", nonpos.height, nonpos["ts"].head(5).to_list())
    _closed_market_check(df, rep, hours)
    return rep


def _base_report(df: pl.DataFrame, kind: str, tf: str) -> Report:
    ts = df["ts"].drop_nulls() if df.height else df["ts"]
    return Report(kind, tf, df.height,
                  str(ts.min()) if ts.len() else None, str(ts.max()) if ts.len() else None)


def _common_checks(df: pl.DataFrame, rep: Report, conflicting_dupes: bool = True) -> None:
    nulls = df["ts"].null_count()
    if nulls:
        rep.add(ERROR, "null_timestamp",
                "Unparseable or non-existent local time (DST gap?) in source timezone", nulls)
    if not df["ts"].drop_nulls().is_sorted():
        rep.add(ERROR, "unsorted", "Timestamps not in ascending order")
    dup = df.filter(pl.col("ts").is_duplicated())
    if dup.height and conflicting_dupes:
        rep.add(ERROR, "conflicting_duplicates", "Same timestamp with different values",
                dup.height, dup["ts"].unique().sort().head(5).to_list())
    elif dup.height:
        rep.add(INFO, "same_timestamp", "Multiple rows share a timestamp", dup.height)


def _closed_market_check(df: pl.DataFrame, rep: Report, hours: MarketHours) -> None:
    closed = df.filter(hours.closed(pl.col("ts")))
    if closed.height:
        rep.add(WARNING, "data_in_closed_market",
                "Rows during daily break/weekend: timezone may be wrong", closed.height,
                closed["ts"].head(5).to_list())


def _timezone_check(df: pl.DataFrame, rep: Report, hours: MarketHours) -> None:
    """The emptiest local hour (Mon-Thu) should be the daily break. Checked per month to catch DST errors."""
    agg = (
        df.select(local=pl.col("ts").dt.convert_time_zone(hours.tz))
        .filter(pl.col("local").dt.weekday() <= 4)
        .group_by(month=pl.col("local").dt.strftime("%Y-%m"), hour=pl.col("local").dt.hour())
        .len()
    )
    counts: dict[str, dict[int, int]] = defaultdict(lambda: dict.fromkeys(range(24), 0))
    for row in agg.iter_rows(named=True):
        counts[row["month"]][row["hour"]] = row["len"]
    expected = hours.break_start // 60
    wrong = {}
    for month, by_hour in sorted(counts.items()):
        if sum(by_hour.values()) < 24 * 5:  # too little data to judge
            continue
        quietest = min(by_hour, key=lambda h: (by_hour[h], h != expected))
        if quietest != expected:
            wrong[month] = (quietest - expected + 12) % 24 - 12
    if wrong:
        offsets = sorted(set(wrong.values()))
        rep.add(ERROR, "timezone_suspect",
                f"Daily break not at {hours.tz} {expected}:00 in {len(wrong)} month(s); "
                f"apparent offset(s) {offsets} h. Check the source timezone / DST handling.",
                len(wrong), [f"{m}: {o:+d}h" for m, o in list(wrong.items())[:5]])


def _gap_check(df: pl.DataFrame, rep: Report, hours: MarketHours, tf: str, tf_min: int) -> None:
    ts = df["ts"].drop_nulls()
    grid = pl.DataFrame({
        "ts": pl.datetime_range(ts.min(), ts.max(), interval=TIMEFRAMES[tf], eager=True,
                                time_zone="UTC", time_unit="us")
    }).filter(~hours.closed(pl.col("ts"), tolerant=True))
    missing = grid.join(df.select("ts"), on="ts", how="anti").sort("ts")
    expected = grid.height
    if missing.height == 0:
        rep.gaps = {"expected_bars": expected, "missing_bars": 0, "gaps": 0}
        return
    step = pl.duration(minutes=tf_min)
    gaps = (
        missing.with_columns(gid=(pl.col("ts").diff() != step).fill_null(True).cum_sum())
        .group_by("gid")
        .agg(start=pl.col("ts").min(), bars=pl.len())
        .with_columns(minutes=pl.col("bars") * tf_min)
        .sort("bars", descending=True)
    )
    buckets = {
        "1_bar": gaps.filter(pl.col("bars") == 1).height,
        "2_5_bars": gaps.filter(pl.col("bars").is_between(2, 5)).height,
        "6_59_min": gaps.filter((pl.col("bars") > 5) & (pl.col("minutes") < 60)).height,
        "60min_plus": gaps.filter(pl.col("minutes") >= 60).height,
    }
    largest = [{"start": str(r["start"]), "minutes": r["minutes"]}
               for r in gaps.head(10).iter_rows(named=True)]
    rep.gaps = {
        "expected_bars": expected,
        "missing_bars": missing.height,
        "missing_pct": round(100 * missing.height / expected, 3),
        "gaps": gaps.height,
        "buckets": buckets,
        "largest": largest,
    }
    if buckets["60min_plus"]:
        rep.add(WARNING, "large_gaps",
                "Gaps >= 60 min while market should be open (holidays or missing data)",
                buckets["60min_plus"], [f"{g['start']} ({g['minutes']} min)" for g in largest])
    small = gaps.height - buckets["60min_plus"]
    if small:
        rep.add(INFO, "small_gaps", "Short gaps (quiet periods are normal on M1)", small)
