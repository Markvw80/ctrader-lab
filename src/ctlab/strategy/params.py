"""Typed strategy parameters with a search range (used for validation and by the optimizer).

    class MyStrategy(Strategy):
        lookback = IntParam(20, 5, 100, doc="bars in the moving average")
        entry_z = FloatParam(2.0, 1.0, 3.5, step=0.1)
        use_filter = BoolParam(True)
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Param:
    kind: str                       # "int" | "float" | "choice"
    default: Any
    low: float | None = None
    high: float | None = None
    step: float | None = None
    log: bool = False
    choices: tuple | None = None
    doc: str = ""

    def validate(self, name: str, value: Any) -> Any:
        if self.kind == "choice":
            if value not in self.choices:
                raise ValueError(f"{name}={value!r} not in {self.choices}")
            return value
        if isinstance(value, bool):
            raise TypeError(f"{name} must be {self.kind}, got bool")
        if self.kind == "int":
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if not isinstance(value, int):
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
        else:
            if not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be float, got {type(value).__name__}")
            value = float(value)
        if not (self.low <= value <= self.high):
            raise ValueError(f"{name}={value} outside range [{self.low}, {self.high}]")
        return value

    def parse(self, text: str) -> Any:
        """Parse a CLI value (`--param name=value`)."""
        if self.kind == "int":
            return int(text)
        if self.kind == "float":
            return float(text)
        for c in self.choices:
            if str(c).lower() == text.lower():
                return c
        raise ValueError(f"{text!r} not in {self.choices}")


def IntParam(default: int, low: int, high: int, step: int = 1, doc: str = "") -> Any:
    return Param("int", default, low, high, step, doc=doc)


def FloatParam(default: float, low: float, high: float, step: float | None = None,
               log: bool = False, doc: str = "") -> Any:
    return Param("float", float(default), float(low), float(high), step, log, doc=doc)


def ChoiceParam(default: Any, choices: tuple, doc: str = "") -> Any:
    return Param("choice", default, choices=tuple(choices), doc=doc)


def BoolParam(default: bool, doc: str = "") -> Any:
    return Param("choice", default, choices=(False, True), doc=doc)
