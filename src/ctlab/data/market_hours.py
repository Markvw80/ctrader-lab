"""Market open/closed classification for gap detection and timezone sanity checks."""

from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class MarketHours:
    tz: str = "America/New_York"
    break_start: int = 17 * 60   # minutes after local midnight
    break_end: int = 18 * 60
    tolerance: int = 5
    spike_pct: float = 2.0

    @classmethod
    def from_spec(cls, spec: dict) -> "MarketHours":
        mh = spec.get("market_hours", {})
        start, end = mh.get("daily_break", ["17:00", "18:00"])
        return cls(
            tz=mh.get("tz", "America/New_York"),
            break_start=_minutes(start),
            break_end=_minutes(end),
            tolerance=int(mh.get("edge_tolerance_minutes", 5)),
            spike_pct=float(mh.get("spike_pct", 2.0)),
        )

    def closed(self, ts: pl.Expr, tolerant: bool = False) -> pl.Expr:
        """True when `ts` (UTC) falls in the daily break or the weekend.

        tolerant=True widens the closed window by `tolerance` minutes on both sides.
        Weekend: Friday from break start until Sunday break end (local time).
        """
        pad = self.tolerance if tolerant else 0
        bs, be = self.break_start - pad, self.break_end + pad
        local = ts.dt.convert_time_zone(self.tz)
        wd = local.dt.weekday()  # Mon=1 .. Sun=7
        mod = local.dt.hour().cast(pl.Int32) * 60 + local.dt.minute().cast(pl.Int32)
        return (
            ((wd == 5) & (mod >= bs))
            | (wd == 6)
            | ((wd == 7) & (mod < be))
            | ((mod >= bs) & (mod < be))
        )


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)
