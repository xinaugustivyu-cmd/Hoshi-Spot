"""模拟权益和仓位预算计算；现金已经包含实际入场手续费。"""

from __future__ import annotations

import math

from .models import EquityInput, RiskPolicy


def simulated_equity(value: EquityInput) -> float:
    if not math.isfinite(value.cash_usdt) or value.cash_usdt < 0:
        raise ValueError("模拟现金无效")
    total = value.cash_usdt
    for position in value.positions:
        numbers = (position.quantity, position.price, position.estimated_exit_fee_fraction,
                   position.entry_cost_usdt)
        if not all(math.isfinite(item) for item in numbers):
            raise ValueError("模拟持仓估值不是有限值")
        if (position.quantity <= 0 or position.price <= 0 or position.entry_cost_usdt <= 0
                or not 0 <= position.estimated_exit_fee_fraction < 1):
            raise ValueError("模拟持仓估值无效")
        total += position.quantity * position.price * (1 - position.estimated_exit_fee_fraction)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("模拟账户权益无效")
    return total


def planned_stake(
    *, available_cash: float, equity: float, open_trades: int,
    policy: RiskPolicy, leverage: float = 1.0, trading_mode: str = "spot",
) -> float:
    if trading_mode != "spot" or leverage != 1.0 or open_trades < 0 or open_trades >= policy.max_open_trades:
        return 0.0
    if not all(math.isfinite(v) and v > 0 for v in (available_cash, equity)):
        return 0.0
    estimated_loss_per_stake = policy.stoploss_fraction + 2 * (
        policy.fee_per_side + policy.slippage_budget_per_side
    )
    if estimated_loss_per_stake <= 0:
        return 0.0
    return max(0.0, min(
        policy.max_stake_usdt,
        available_cash,
        equity * policy.max_position_fraction,
        equity * policy.max_trade_risk_fraction / estimated_loss_per_stake,
    ))
