"""研究建议与人工意见账本。没有交易所对象、密钥或订单执行方法。"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("审批时间必须带 UTC 时区")
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class ResearchAdvice:
    advice_id: str
    account_id: str
    strategy_version: str
    signal_id: str
    market_event_id: str
    signal_candle_close_utc: datetime
    generated_at_utc: datetime
    expires_at_utc: datetime
    pair: str
    reference_price: float
    planned_stake_usdt: float
    planned_take_profit: float
    planned_stop_loss: float
    reason: str
    risk_state_id: str
    risk_policy_hash: str


class ApprovalStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def initialize(self) -> None:
        if self.path.exists():
            raise ValueError("审批数据库已存在，禁止覆盖")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.executescript("""
                CREATE TABLE advice (
                    advice_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL, signal_id TEXT NOT NULL,
                    market_event_id TEXT NOT NULL, signal_candle_close_utc TEXT NOT NULL,
                    generated_at_utc TEXT NOT NULL, expires_at_utc TEXT NOT NULL,
                    details_json TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE reviews (
                    approval_id TEXT PRIMARY KEY, advice_id TEXT UNIQUE NOT NULL,
                    reviewer TEXT NOT NULL, reviewed_at_utc TEXT NOT NULL,
                    decision TEXT NOT NULL, reason TEXT NOT NULL,
                    FOREIGN KEY(advice_id) REFERENCES advice(advice_id)
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise RuntimeError("审批数据库缺失，不自动重建")
        connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=rw", uri=True)
        row = connection.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            connection.close()
            raise RuntimeError("审批数据库损坏")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def propose(self, advice: ResearchAdvice) -> None:
        created = utc_text(advice.generated_at_utc)
        close = utc_text(advice.signal_candle_close_utc)
        expiry = utc_text(advice.expires_at_utc)
        if (not advice.advice_id or not advice.signal_id or not advice.market_event_id
                or advice.account_id not in {"A", "B", "C"}
                or advice.strategy_version != f"HoshiSpotV11{advice.account_id}"
                or advice.pair not in {"BTC/USDT", "ETH/USDT"}
                or advice.generated_at_utc < advice.signal_candle_close_utc
                or advice.expires_at_utc <= advice.generated_at_utc
                or advice.expires_at_utc - advice.generated_at_utc > timedelta(minutes=15)
                or not all(math.isfinite(value) for value in (
                    advice.planned_stake_usdt, advice.reference_price,
                    advice.planned_take_profit, advice.planned_stop_loss))
                or not 0 < advice.planned_stake_usdt <= 16
                or advice.reference_price <= 0
                or abs(advice.planned_take_profit / advice.reference_price - 1.06) > 1e-6
                or abs(advice.planned_stop_loss / advice.reference_price - 0.96) > 1e-6
                or not advice.risk_state_id or not advice.risk_policy_hash or not advice.reason):
            raise ValueError("研究建议字段无效")
        details = asdict(advice)
        for key in ("signal_candle_close_utc", "generated_at_utc", "expires_at_utc"):
            details[key] = utc_text(details[key])
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO advice VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')",
                (advice.advice_id, advice.account_id, advice.strategy_version,
                 advice.signal_id, advice.market_event_id, close, created,
                 expiry, json.dumps(details, ensure_ascii=False, sort_keys=True)),
            )

    def status(self, advice_id: str, now: datetime) -> str:
        point = utc_text(now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, expires_at_utc FROM advice WHERE advice_id=?", (advice_id,)
            ).fetchone()
            if row is None:
                raise KeyError(advice_id)
            if row[0] == "PENDING" and row[1] <= point:
                connection.execute("UPDATE advice SET status='EXPIRED' WHERE advice_id=?", (advice_id,))
                return "EXPIRED"
            return row[0]

    def review(self, *, approval_id: str, advice_id: str, reviewer: str,
               reviewed_at: datetime, decision: str, reason: str) -> str:
        point = utc_text(reviewed_at)
        if decision not in {"APPROVED", "REJECTED"} or not all((approval_id, reviewer, reason)):
            raise ValueError("审批决定或记录不完整")
        if self.status(advice_id, reviewed_at) != "PENDING":
            raise ValueError("建议已审批或过期")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
                (approval_id, advice_id, reviewer, point, decision, reason),
            )
            connection.execute("UPDATE advice SET status=? WHERE advice_id=?",
                               (decision, advice_id))
        return decision
