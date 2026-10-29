"""规则型研究建议接口。不含交易所客户端，也不提供下单函数。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TradeSuggestion:
    pair: str
    generated_at_utc: str
    valid_until_utc: str
    reference_price: float
    stake_usdt: float
    take_profit_price: float
    stop_loss_price: float
    reasons: list[str]
    risks: list[str]
    simulated_only: bool = True
    requires_human_review: bool = True


def explain_candidate(pair: str, candle: dict[str, Any], stake_usdt: float, now: datetime) -> TradeSuggestion:
    """将确定性信号翻译为简短说明，不会调用任何交易接口。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必须带时区")
    price = float(candle["close"])
    return TradeSuggestion(
        pair=pair,
        generated_at_utc=now.astimezone(timezone.utc).isoformat(),
        valid_until_utc=(now.astimezone(timezone.utc) + timedelta(minutes=15)).isoformat(),
        reference_price=price,
        stake_usdt=round(stake_usdt, 2),
        take_profit_price=round(price * 1.06, 8),
        stop_loss_price=round(price * 0.96, 8),
        reasons=["4小时趋势满足 EMA20 > EMA50 且收盘高于 EMA50", "1小时 RSI 上穿 50 且不高于 65", "1小时收盘价上穿 EMA20", "成交量及不追涨条件通过"],
        risks=["止损价不保证成交；历史或模拟成交不代表未来结果", "重大事件、交易所最低订单金额和实时价格偏移仍须复核"],
    )


def append_record(path: str | Path, record: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def record_human_review(path: str | Path, suggestion_id: str, decision: str, note: str = "") -> None:
    if decision not in {"approved_for_manual_review", "rejected", "expired"}:
        raise ValueError("decision 值不受支持")
    append_record(path, {
        "suggestion_id": suggestion_id,
        "reviewed_at_utc": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "note": note,
        "this_does_not_authorize_or_submit_orders": True,
    })

