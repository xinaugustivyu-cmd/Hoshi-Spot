"""风控输入、输出和固定参数；不持有交易所或下单接口。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(frozen=True)
class RiskPolicy:
    version: str = "hoshi-v1.1-risk-1"
    max_stake_usdt: float = 16.0
    max_position_fraction: float = 0.20
    max_trade_risk_fraction: float = 0.01
    stoploss_fraction: float = 0.04
    fee_per_side: float = 0.001
    slippage_budget_per_side: float = 0.0005
    weekly_loss_fraction: float = 0.03
    rolling_14d_loss_fraction: float = 0.05
    max_drawdown_fraction: float = 0.05
    max_open_trades: int = 1

    def __post_init__(self) -> None:
        values = (
            self.max_stake_usdt, self.max_position_fraction,
            self.max_trade_risk_fraction, self.stoploss_fraction,
            self.fee_per_side, self.slippage_budget_per_side,
            self.weekly_loss_fraction, self.rolling_14d_loss_fraction,
            self.max_drawdown_fraction,
        )
        if not all(math.isfinite(value) and value >= 0 for value in values):
            raise ValueError("风控参数必须为有限非负值")
        if (self.max_stake_usdt > 16 or self.max_position_fraction > 0.20
                or self.max_trade_risk_fraction > 0.01 or self.stoploss_fraction != 0.04
                or self.weekly_loss_fraction > 0.03
                or self.rolling_14d_loss_fraction > 0.05
                or self.max_drawdown_fraction > 0.05 or self.max_open_trades != 1):
            raise ValueError("V1.1 不允许放宽已批准风控上限")

    @property
    def hash(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ClosedTrade:
    trade_id: str
    closed_at: datetime
    net_profit_usdt: float
    exit_reason: str


@dataclass(frozen=True)
class PositionMark:
    pair: str
    quantity: float
    price: float
    estimated_exit_fee_fraction: float
    entry_cost_usdt: float


@dataclass(frozen=True)
class EquityInput:
    cash_usdt: float
    positions: tuple[PositionMark, ...] = ()


@dataclass(frozen=True)
class RiskDecision:
    account_id: str
    state: str
    state_id: str
    triggered_at: datetime
    rule: str
    details: dict
    pause_until: datetime | None
    approval_id: str | None
    allow_new_entry: bool
    allow_exit: bool
    auto_resume: bool
    policy_version: str
    policy_hash: str


STATE_PRIORITY = {
    "ACTIVE": 0,
    "COOLDOWN": 1,
    "DATA_PAUSE": 2,
    "WEEKLY_PAUSE": 3,
    "HALTED_MANUAL_REVIEW": 4,
    "EMERGENCY_HALT": 5,
}
