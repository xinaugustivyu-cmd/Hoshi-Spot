"""与 Freqtrade 解耦的确定性风险检查；遇到缺失或异常输入时拒绝新建议。"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class RiskSettings:
    max_position_fraction: float = 0.20
    max_loss_fraction: float = 0.01
    stoploss_fraction: float = 0.04
    fee_per_side: float = 0.001
    slippage_per_side: float = 0.0005
    max_signal_age_minutes: int = 15
    max_entry_deviation: float = 0.003
    max_candle_change: float = 0.10
    daily_stoploss_limit: int = 1
    weekly_loss_limit: float = 0.03
    stage_loss_limit_fraction: float = 0.05


@dataclass(frozen=True)
class TradeResult:
    close_date_utc: datetime
    close_profit_abs: float
    exit_reason: str


def planned_stake(available_balance: float, account_equity: float, settings: RiskSettings) -> float:
    """按 20% 名义仓位与 1% 账户风险上限中较小者计算。"""
    if available_balance <= 0 or account_equity <= 0:
        return 0.0
    round_trip_cost = 2 * (settings.fee_per_side + settings.slippage_per_side)
    risk_per_stake = settings.stoploss_fraction + round_trip_cost
    risk_cap = account_equity * settings.max_loss_fraction / risk_per_stake
    return max(0.0, min(available_balance, account_equity * settings.max_position_fraction, risk_cap))


def validate_market(
    *,
    candle_time: datetime,
    current_time: datetime,
    reference_price: float,
    current_price: float,
    candle_change: float,
    signal_created_at: datetime | None = None,
    settings: RiskSettings = RiskSettings(),
) -> str | None:
    """返回拦截原因；None 表示市场数据和建议时效通过。时间必须带时区。"""
    timestamps = [candle_time, current_time]
    if signal_created_at is not None:
        timestamps.append(signal_created_at)
    if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
        return "时间戳缺少时区"
    if reference_price <= 0 or current_price <= 0:
        return "价格无效"
    if not all(map(lambda value: value == value and abs(value) != float("inf"), [reference_price, current_price, candle_change])):
        return "行情包含非有限值"
    if current_time.astimezone(timezone.utc) < candle_time.astimezone(timezone.utc):
        return "行情时间戳位于未来"
    if (current_time - candle_time).total_seconds() > 75 * 60:
        return "K线数据过期"
    if abs(candle_change) > settings.max_candle_change:
        return "单根K线波动超过异常行情阈值"
    deviation = abs(current_price / reference_price - 1)
    if deviation > settings.max_entry_deviation:
        return "现价偏离参考入场价超过阈值"
    if signal_created_at is not None:
        age = current_time.astimezone(timezone.utc) - signal_created_at.astimezone(timezone.utc)
        if age < timedelta(0) or age > timedelta(minutes=settings.max_signal_age_minutes):
            return "信号已过期"
    return None


def read_event_blackouts(path: str | Path) -> list[tuple[datetime, datetime, str]]:
    windows: list[tuple[datetime, datetime, str]] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            start = datetime.fromisoformat(row["start_utc"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(row["end_utc"].replace("Z", "+00:00"))
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                raise ValueError("事件暂停区间必须是有效的带时区 UTC 起止时间")
            windows.append((start.astimezone(timezone.utc), end.astimezone(timezone.utc), row["event"]))
    return windows


def event_blackout_reason(now: datetime, windows: Iterable[tuple[datetime, datetime, str]]) -> str | None:
    if now.tzinfo is None or now.utcoffset() is None:
        return "当前时间缺少时区"
    point = now.astimezone(timezone.utc)
    for start, end, event in windows:
        if start <= point < end:
            return f"重大事件暂停新开仓：{event}"
    return None


def loss_limit_reason(
    *,
    now: datetime,
    closed_trades: Iterable[TradeResult],
    account_equity: float,
    stage_initial_equity: float = 80.0,
    settings: RiskSettings = RiskSettings(),
) -> str | None:
    if (
        now.tzinfo is None
        or now.utcoffset() is None
        or account_equity <= 0
        or stage_initial_equity <= 0
    ):
        return "账户风险数据无效"
    utc_now = now.astimezone(timezone.utc)
    day_start = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = (utc_now - timedelta(days=utc_now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    trades = list(closed_trades)
    if any(t.exit_reason.lower() in {"stop_loss", "stoploss"} and t.close_date_utc.astimezone(timezone.utc) >= day_start for t in trades):
        return "本 UTC 日出现止损，停止新增交易"
    week_pnl = sum(t.close_profit_abs for t in trades if t.close_date_utc.astimezone(timezone.utc) >= week_start)
    week_opening_equity = max(0.0, account_equity - week_pnl)
    if week_pnl <= -week_opening_equity * settings.weekly_loss_limit:
        return "本周累计亏损达到 3% 限额"
    stage_pnl = sum(t.close_profit_abs for t in trades)
    if stage_pnl <= -stage_initial_equity * settings.stage_loss_limit_fraction:
        return "阶段累计亏损达到初始实验本金的 5%，终止本阶段新增交易"
    return None


def minimum_order_reason(stake: float, minimum_stake: float | None, amount_step: float | None = None, price: float | None = None) -> str | None:
    if stake <= 0:
        return "计算仓位为零"
    if minimum_stake is not None and stake < minimum_stake:
        return "模拟仓位低于交易所最小名义金额"
    if amount_step is not None and price is not None and amount_step > 0:
        quantity = math.floor((stake / price + 1e-12) / amount_step) * amount_step
        if quantity < amount_step:
            return "模拟数量低于交易对数量精度要求"
        if minimum_stake is not None and quantity * price < minimum_stake:
            return "按交易对数量精度舍入后低于最小名义金额"
    return None


def within_open_trade_limit(current_open_trades: int, maximum_open_trades: int) -> bool:
    return maximum_open_trades > 0 and current_open_trades < maximum_open_trades

