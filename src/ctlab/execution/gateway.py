"""Order layer interface. Not implemented in the research phase.

Any future implementation must call `assert_demo()` before sending anything.
"""

from typing import Protocol

from ctlab.config import env


class LiveTradingDisabled(RuntimeError):
    pass


def assert_demo() -> None:
    if env().ctrader_env != "demo":
        raise LiveTradingDisabled(
            f"CTRADER_ENV={env().ctrader_env!r}: order layer only runs against a demo account."
        )


class OrderGateway(Protocol):
    def market_order(self, symbol: str, side: str, volume_lots: float,
                     stop_loss: float | None, take_profit: float | None) -> str: ...

    def close_position(self, position_id: str) -> None: ...
