"""Strategy base class and the context a strategy sees on every closed bar.

Contract (enforced by the engine and by ctlab.strategy.causality):
- `prepare(bars)` computes indicators vectorized over the whole history. Every value at row i
  may depend only on rows <= i. The causality check verifies this automatically.
- `on_bar(ctx)` is called once per CLOSED strategy bar. `ctx` only exposes data up to and
  including that bar. Orders are executed by the engine afterwards, never on that same bar.
- Do not keep references to the full `prepare` output on `self`; read through `ctx`.
"""

from dataclasses import dataclass
from typing import ClassVar

import numpy as np
import polars as pl

from ctlab.strategy.params import Param

LONG, SHORT = 1, -1


@dataclass
class OrderRequest:
    side: int                       # LONG | SHORT
    kind: str                       # "market" | "stop" | "limit"
    sl_distance: float              # price distance from the actual fill
    tp_distance: float | None       # None = no take profit
    price: float | None = None      # trigger price for stop/limit (buy: ask, sell: bid)
    expire_bars: int | None = None  # pending order lifetime in strategy bars
    tag: str = ""


@dataclass(frozen=True)
class PositionInfo:
    side: int
    lots: float
    entry_price: float
    sl: float
    tp: float | None
    entry_ts: np.datetime64
    tag: str


class Series:
    """Attribute access to columns sliced up to the current bar: ctx.bars.close[-1]."""

    __slots__ = ("_cols", "_end")

    def __init__(self, cols: dict[str, np.ndarray], end: int):
        self._cols, self._end = cols, end

    def __getattr__(self, name: str) -> np.ndarray:
        try:
            return self._cols[name][: self._end]
        except KeyError:
            raise AttributeError(name) from None

    def __getitem__(self, name: str) -> np.ndarray:
        return self._cols[name][: self._end]


class Context:
    """What a strategy may see and do at the close of strategy bar `i`."""

    def __init__(self, i: int, cols: dict[str, np.ndarray], position: PositionInfo | None,
                 has_pending: bool, equity: float, halted: bool):
        self.i = i
        self.bars = Series(cols, i + 1)
        self.position = position
        self.has_pending = has_pending
        self.equity = equity
        self.halted = halted
        self.orders: list[OrderRequest] = []
        self.close_requested = False
        self.cancel_requested = False

    def now(self, name: str):
        """Value of column `name` on the current (just closed) bar."""
        return self.bars[name][-1]

    # --- actions (executed by the engine on the next execution bar) ---
    def buy(self, sl_distance: float, tp_distance: float | None = None, tag: str = "") -> None:
        self.orders.append(OrderRequest(LONG, "market", sl_distance, tp_distance, tag=tag))

    def sell(self, sl_distance: float, tp_distance: float | None = None, tag: str = "") -> None:
        self.orders.append(OrderRequest(SHORT, "market", sl_distance, tp_distance, tag=tag))

    def buy_stop(self, price: float, sl_distance: float, tp_distance: float | None = None,
                 expire_bars: int | None = None, tag: str = "") -> None:
        self.orders.append(OrderRequest(LONG, "stop", sl_distance, tp_distance, price, expire_bars, tag))

    def sell_stop(self, price: float, sl_distance: float, tp_distance: float | None = None,
                  expire_bars: int | None = None, tag: str = "") -> None:
        self.orders.append(OrderRequest(SHORT, "stop", sl_distance, tp_distance, price, expire_bars, tag))

    def buy_limit(self, price: float, sl_distance: float, tp_distance: float | None = None,
                  expire_bars: int | None = None, tag: str = "") -> None:
        self.orders.append(OrderRequest(LONG, "limit", sl_distance, tp_distance, price, expire_bars, tag))

    def sell_limit(self, price: float, sl_distance: float, tp_distance: float | None = None,
                   expire_bars: int | None = None, tag: str = "") -> None:
        self.orders.append(OrderRequest(SHORT, "limit", sl_distance, tp_distance, price, expire_bars, tag))

    def close(self) -> None:
        self.close_requested = True

    def cancel_all(self) -> None:
        self.cancel_requested = True


class Strategy:
    """Base class. Subclasses declare Params as class attributes and implement prepare/on_bar."""

    name: ClassVar[str] = ""
    timeframe: ClassVar[str] = "M15"
    warmup_bars: ClassVar[int] = 0      # no on_bar calls before this bar index

    def __init__(self, **params):
        specs = self.param_specs()
        unknown = set(params) - set(specs)
        if unknown:
            raise ValueError(f"Unknown parameter(s) for {self.name}: {sorted(unknown)}")
        self.params = {}
        for key, spec in specs.items():
            value = spec.validate(key, params.get(key, spec.default))
            setattr(self, key, value)
            self.params[key] = value

    @classmethod
    def param_specs(cls) -> dict[str, Param]:
        specs: dict[str, Param] = {}
        for klass in reversed(cls.__mro__):
            for key, value in vars(klass).items():
                if isinstance(value, Param):
                    specs[key] = value
        return specs

    def prepare(self, bars: pl.DataFrame) -> dict[str, np.ndarray]:
        """Return extra columns (indicators). Must be causal. Default: none."""
        return {}

    def on_bar(self, ctx: Context) -> None:
        raise NotImplementedError


_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} needs a `name`")
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy(name: str) -> type[Strategy]:
    _load_builtin()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"Unknown strategy {name!r}; available: {sorted(_REGISTRY)}") from None


def all_strategies() -> dict[str, type[Strategy]]:
    _load_builtin()
    return dict(_REGISTRY)


def _load_builtin() -> None:
    import importlib
    import pkgutil

    import ctlab.strategies as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"{pkg.__name__}.{mod.name}")
