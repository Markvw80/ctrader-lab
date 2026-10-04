"""Cost model per symbol: spread, commission, swap, slippage. All amounts in account currency (USD).

Source: config/symbols/<SYMBOL>.yaml, overridden by broker values fetched from the API.
"""

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import polars as pl

from ctlab.data.timezones import NY

WEEKDAY_NUM = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
               "saturday": 5, "sunday": 6}


@dataclass(frozen=True)
class CostModel:
    contract_size: float               # units per 1.00 lot (XAUUSD: 100 oz)
    tick_size: float
    pip_size: float
    min_lot: float
    lot_step: float
    commission_type: str               # usd_per_million_notional | usd_per_lot | percentage_of_value
    commission_value: float            # per side
    slippage_ticks: float
    swap_calculation: str              # usd_per_lot | pips | percentage
    swap_long: float
    swap_short: float
    triple_day: int                    # 0=Mon .. 6=Sun (weekday of the trading day that ends)
    spread_hours: dict[int, float] = field(default_factory=dict)  # NY hour -> spread (price)
    spread_default: float = 0.2

    @classmethod
    def from_spec(cls, spec: dict, spread_stat: str = "median") -> "CostModel":
        c = spec["costs"]
        sp = c.get("spread", {})
        tick = float(spec["tick_size"])
        hours = {}
        if sp.get("hours"):
            hours = {int(h): float(v[spread_stat]) for h, v in sp["hours"].items()}
        return cls(
            contract_size=float(spec["contract_size"]),
            tick_size=tick,
            pip_size=float(spec["pip_size"]),
            min_lot=float(spec["min_lot"]),
            lot_step=float(spec["lot_step"]),
            commission_type=c["commission"]["type"],
            commission_value=float(c["commission"]["value_per_side"]),
            slippage_ticks=float(c.get("slippage_ticks", 0)),
            swap_calculation=c["swap"].get("calculation", "usd_per_lot"),
            swap_long=float(c["swap"]["long"]),
            swap_short=float(c["swap"]["short"]),
            triple_day=WEEKDAY_NUM[c["swap"].get("triple_day", "wednesday")],
            spread_hours=hours,
            spread_default=float(sp.get("default_points", 20)) * tick,
        )

    @property
    def slippage(self) -> float:
        return self.slippage_ticks * self.tick_size

    def pip_value_per_lot(self) -> float:
        """USD per pip per 1.00 lot (quote currency = USD)."""
        return self.pip_size * self.contract_size

    def commission(self, lots: float, price: float) -> float:
        """Commission for ONE side (open or close)."""
        notional = lots * self.contract_size * price
        if self.commission_type == "usd_per_million_notional":
            return self.commission_value * notional / 1e6
        if self.commission_type in ("usd_per_lot", "quote_ccy_per_lot"):
            return self.commission_value * lots
        if self.commission_type == "percentage_of_value":
            return self.commission_value / 100 * notional
        raise ValueError(f"Unsupported commission type {self.commission_type}")

    def swap_per_night(self, side: int, lots: float, price: float) -> float:
        """Swap for one rollover (positive = credit, negative = cost)."""
        rate = self.swap_long if side > 0 else self.swap_short
        if self.swap_calculation == "usd_per_lot":
            return rate * lots
        if self.swap_calculation == "pips":
            return rate * self.pip_size * self.contract_size * lots
        if self.swap_calculation == "percentage":  # annual %, 360-day year
            return rate / 100 * lots * self.contract_size * price / 360
        raise ValueError(f"Unsupported swap calculation {self.swap_calculation}")

    def swap_nights(self, from_day: int, to_day: int) -> int:
        """Rollovers charged when moving from trading day `from_day` to `to_day` (day numbers).

        Each weekday (Mon-Fri) trading day that ends counts once; the triple day counts three times.
        """
        n = 0
        for d in range(from_day, to_day):
            wd = date.fromordinal(d + 719163).weekday()  # 719163 = ordinal of 1970-01-01
            if wd < 5:
                n += 3 if wd == self.triple_day else 1
        return n

    def spread_series(self, ts: pl.Series) -> np.ndarray:
        """Spread (price units) per bar, by New York hour of the bar open."""
        hours = ts.dt.convert_time_zone(NY).dt.hour().to_numpy()
        if not self.spread_hours:
            return np.full(len(ts), self.spread_default)
        lut = np.array([self.spread_hours.get(h, self.spread_default) for h in range(24)])
        return lut[hours]
