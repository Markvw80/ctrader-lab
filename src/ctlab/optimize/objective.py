"""Objective functions on backtest metrics (higher is better)."""

REJECTED = -1e6   # value for trials that violate a constraint (too few trades, no result)


def objective_value(metrics: dict, name: str, initial_balance: float) -> float:
    if name == "sharpe":
        v = metrics.get("sharpe")
        return REJECTED if v is None else float(v)
    if name == "net_profit":
        return float(metrics["net_profit"])
    if name == "return_dd":
        dd = max(metrics["max_drawdown"], 0.01 * initial_balance)
        return float(metrics["net_profit"]) / dd
    if name == "profit_factor":
        pf = metrics.get("profit_factor")
        return 5.0 if pf is None and metrics["net_profit"] > 0 else min(pf or 0.0, 5.0)
    raise ValueError(f"Unknown objective {name!r}")


def annual_return_pct(metrics: dict, years: float) -> float:
    return metrics["return_pct"] / years if years > 0 else 0.0
