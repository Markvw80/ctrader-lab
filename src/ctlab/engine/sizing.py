"""Position size from risk per trade."""

import math

from ctlab.engine.costs import CostModel


def lots_for_risk(cost: CostModel, equity: float, risk_pct: float, sl_distance: float,
                  price: float, max_leverage: float) -> float:
    """Largest lot size (rounded DOWN to lot_step) whose loss at the stop stays within risk.

    Loss per lot at the stop = SL distance + exit slippage, plus commission for both sides.
    Also capped by notional <= equity * max_leverage. Returns 0.0 if below the minimum lot.
    """
    if sl_distance <= 0 or equity <= 0:
        return 0.0
    risk = equity * risk_pct / 100
    loss_per_lot = ((sl_distance + cost.slippage) * cost.contract_size
                    + 2 * cost.commission(1.0, price))
    lots = risk / loss_per_lot
    lots = min(lots, equity * max_leverage / (cost.contract_size * price))
    steps = math.floor(lots / cost.lot_step + 1e-9)
    lots = round(steps * cost.lot_step, 8)
    return lots if lots >= cost.min_lot - 1e-12 else 0.0
