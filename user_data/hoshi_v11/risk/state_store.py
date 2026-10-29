"""每个模拟账户独立的 SQLite 审计账本；缺失或损坏时拒绝隐式重建。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUSY_TIMEOUT_MS = 5000


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE closed_trades (
  trade_id TEXT PRIMARY KEY, stage_id TEXT NOT NULL, closed_at TEXT NOT NULL,
  net_profit_usdt REAL NOT NULL, exit_reason TEXT NOT NULL
);
CREATE TABLE week_snapshots (
  week_start TEXT PRIMARY KEY, opening_equity REAL NOT NULL, observed_at TEXT NOT NULL
);
CREATE TABLE equity_marks (
  id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, market_event_id TEXT NOT NULL,
  source TEXT NOT NULL, equity REAL NOT NULL, stage_peak REAL NOT NULL,
  stage_drawdown REAL NOT NULL
);
CREATE TABLE risk_events (
  id INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, observed_at TEXT NOT NULL,
  state TEXT NOT NULL, rule TEXT NOT NULL, details_json TEXT NOT NULL,
  policy_hash TEXT NOT NULL
);
CREATE TABLE approvals (
  approval_id TEXT PRIMARY KEY, reviewed_at TEXT NOT NULL, reviewer TEXT NOT NULL,
  reason TEXT NOT NULL, previous_stage TEXT NOT NULL, new_stage TEXT NOT NULL,
  reviewed_equity REAL NOT NULL
);
"""


def iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("风控时间必须含 UTC 时区")
    return value.astimezone(timezone.utc).isoformat()


class RiskStateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def bootstrap(self, *, account_id: str, stage_id: str, initial_equity: float,
                  started_at: datetime, policy_hash: str) -> None:
        if self.path.exists():
            raise RiskStateError("风险账本已存在，禁止初始化覆盖")
        if not account_id or not stage_id or initial_equity <= 0:
            raise ValueError("账户、阶段或初始模拟权益无效")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            connection.executescript(SCHEMA)
            values = {
                "account_id": account_id,
                "policy_hash": policy_hash,
                "stage_id": stage_id,
                "stage_initial_equity": initial_equity,
                "stage_peak_equity": initial_equity,
                "manual_halt": "",
                "manual_halt_at": "",
                "last_state": "ACTIVE",
                "last_state_id": "bootstrap",
                "last_state_at": iso(started_at),
                "last_observed_at": "",
                "last_closed_candle_at": "",
                "last_market_event_id": "",
                "data_pause": False,
                "healthy_candles": 0,
            }
            with connection:
                self.begin_write(connection)
                for key, value in values.items():
                    self.set_meta(connection, key, value)
                week_start = started_at.astimezone(timezone.utc)
                week_start = (week_start.replace(hour=0, minute=0, second=0, microsecond=0)
                              - timedelta(days=week_start.weekday()))
                connection.execute(
                    "INSERT INTO week_snapshots VALUES (?, ?, ?)",
                    (iso(week_start), initial_equity, iso(started_at)),
                )
        except Exception:
            connection.close()
            self.path.unlink(missing_ok=True)
            raise
        finally:
            connection.close()

    def connect(self, *, account_id: str, policy_hash: str) -> sqlite3.Connection:
        if not self.path.is_file():
            raise RiskStateError("风险账本缺失，禁止自动重建")
        connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=rw", uri=True,
                                     timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            check = connection.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise RiskStateError("风险账本完整性检查失败")
            if self.get_meta(connection, "account_id") != account_id:
                raise RiskStateError("风险账本账户不匹配")
            if self.get_meta(connection, "policy_hash") != policy_hash:
                raise RiskStateError("风控版本或参数哈希不匹配")
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def begin_write(connection: sqlite3.Connection) -> None:
        """Acquire the single SQLite writer slot before reading risk state."""
        if connection.in_transaction:
            raise RiskStateError("风险写入事务不得嵌套")
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def get_meta(connection: sqlite3.Connection, key: str):
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise RiskStateError(f"风险账本缺少 {key}")
        return json.loads(row[0])

    @staticmethod
    def set_meta(connection: sqlite3.Connection, key: str, value) -> None:
        connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
        )
