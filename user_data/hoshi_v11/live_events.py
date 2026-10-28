"""A/B/C 共用的公开行情事件账本。仅存储已收盘 K 线，不含交易接口。"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .event_model import MarketEvent, event_digest


PAIRS = ("BTC/USDT", "ETH/USDT")
TIMEFRAMES = ("1h", "4h")
MAX_SIGNAL_AGE = timedelta(minutes=15)


class MarketEventError(ValueError):
    pass


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketEventError("行情时间必须包含时区")
    return value.astimezone(timezone.utc)


def _parse_time(value: str) -> datetime:
    return utc(datetime.fromisoformat(value))


def _validated_candles(candles: dict, decision: datetime) -> dict:
    if set(candles) != set(PAIRS):
        raise MarketEventError("行情快照必须同时包含 BTC 和 ETH")
    clean = {}
    for pair in PAIRS:
        if set(candles[pair]) != set(TIMEFRAMES):
            raise MarketEventError(f"{pair} 必须同时包含 1h 和 4h")
        clean[pair] = {}
        for timeframe in TIMEFRAMES:
            row = candles[pair][timeframe]
            if set(row) != {"close_time_utc", "open", "high", "low", "close", "volume"}:
                raise MarketEventError("K 线字段缺失或多余")
            close_at = _parse_time(row["close_time_utc"])
            expected = (decision if timeframe == "1h" else
                        decision.replace(hour=decision.hour - decision.hour % 4))
            if close_at != expected:
                raise MarketEventError(f"{pair} {timeframe} 尚未收盘、缺失或过期")
            values = {key: float(row[key]) for key in ("open", "high", "low", "close", "volume")}
            if (not all(math.isfinite(value) for value in values.values())
                    or min(values[key] for key in ("open", "high", "low", "close")) <= 0
                    or values["volume"] < 0
                    or values["high"] < max(values["open"], values["close"])
                    or values["low"] > min(values["open"], values["close"])
                    or values["low"] > values["high"]):
                raise MarketEventError(f"{pair} {timeframe} OHLCV 异常")
            clean[pair][timeframe] = {"close_time_utc": close_at.isoformat(), **values}
    return clean


class LiveMarketJournal:
    """生产者显式建库；消费者只读。异常记账后立即使旧快照失效。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def create(self) -> None:
        if self.path.exists():
            raise MarketEventError("共享行情账本已存在，禁止覆盖")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        try:
            db.executescript("""
                CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT INTO meta VALUES ('fault_epoch', 0);
                CREATE TABLE events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  decision_time_utc TEXT UNIQUE NOT NULL,
                  received_at_utc TEXT NOT NULL,
                  event_id TEXT UNIQUE NOT NULL,
                  candles_json TEXT NOT NULL,
                  fault_epoch INTEGER NOT NULL
                );
                CREATE TABLE audit (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  kind TEXT NOT NULL,
                  observed_at_utc TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                );
            """)
        except Exception:
            db.close()
            self.path.unlink(missing_ok=True)
            raise
        finally:
            db.close()

    def _connect(self, *, readonly: bool) -> sqlite3.Connection:
        if not self.path.is_file():
            raise MarketEventError("共享行情账本缺失")
        mode = "ro" if readonly else "rw"
        db = None
        try:
            db = sqlite3.connect(f"file:{self.path.as_posix()}?mode={mode}",
                                 uri=True, timeout=5)
            db.execute("PRAGMA busy_timeout=5000")
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise MarketEventError("共享行情账本损坏")
            db.execute("SELECT value FROM meta WHERE key='fault_epoch'").fetchone()[0]
            return db
        except (MarketEventError, sqlite3.Error, TypeError, IndexError) as exc:
            if db is not None:
                db.close()
            raise MarketEventError(f"共享行情账本不可读：{exc}") from exc

    def fault(self, reason: str, *, detected_at: datetime) -> None:
        moment = utc(detected_at)
        if not reason:
            raise ValueError("故障原因不能为空")
        db = self._connect(readonly=False)
        try:
            with db:
                db.execute("UPDATE meta SET value=value+1 WHERE key='fault_epoch'")
                db.execute("INSERT INTO audit(kind,observed_at_utc,payload_json) VALUES (?,?,?)",
                           ("fault", moment.isoformat(), json.dumps({"reason": reason}, ensure_ascii=False)))
        finally:
            db.close()

    def ingest(self, *, decision_time: datetime, received_at: datetime,
               candles: dict, replay: bool = False) -> MarketEvent:
        decision = utc(decision_time)
        received = utc(received_at)
        try:
            if decision.minute or decision.second or decision.microsecond:
                raise MarketEventError("决策时间不是 UTC 整点")
            if received < decision or (not replay and received - decision > MAX_SIGNAL_AGE):
                raise MarketEventError("公开行情到达时间异常或过期")
            data = _validated_candles(candles, decision)
            event_id = event_digest(decision, data)
            db = self._connect(readonly=False)
            try:
                # Serialize producer decisions as well as writes. Otherwise two
                # producers can both inspect the same latest event before either
                # inserts, leaving the audit trail dependent on scheduling.
                db.execute("BEGIN IMMEDIATE")
                latest = db.execute("SELECT decision_time_utc FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                if latest:
                    last = _parse_time(latest[0])
                    if decision <= last:
                        raise MarketEventError("重复或乱序行情事件")
                    if decision != last + timedelta(hours=1):
                        raise MarketEventError("1h 行情事件缺口，必须顺序补齐")
                epoch = db.execute("SELECT value FROM meta WHERE key='fault_epoch'").fetchone()[0]
                payload = {"decision_time_utc": decision.isoformat(),
                           "received_at_utc": received.isoformat(),
                           "event_id": event_id, "candles": data}
                db.execute("INSERT INTO events(decision_time_utc,received_at_utc,event_id,"
                           "candles_json,fault_epoch) VALUES (?,?,?,?,?)",
                           (decision.isoformat(), received.isoformat(), event_id,
                            json.dumps(data, ensure_ascii=False, sort_keys=True), epoch))
                db.execute("INSERT INTO audit(kind,observed_at_utc,payload_json) VALUES (?,?,?)",
                           ("event", received.isoformat(),
                            json.dumps(payload, ensure_ascii=False, sort_keys=True)))
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            return MarketEvent(event_id, decision, decision, data, received)
        except (MarketEventError, sqlite3.Error, TypeError, KeyError, ValueError) as exc:
            # A corrupt/missing DB cannot persist this fault; every reader still
            # refuses it. Do not let failure logging obscure the original error.
            try:
                self.fault(str(exc), detected_at=received)
            except (MarketEventError, sqlite3.Error, OSError):
                pass
            raise MarketEventError(str(exc)) from exc

    def at(self, decision_time: datetime) -> MarketEvent:
        now = utc(decision_time)
        db = self._connect(readonly=True)
        try:
            row = db.execute("SELECT decision_time_utc,received_at_utc,event_id,candles_json,"
                             "fault_epoch FROM events WHERE decision_time_utc<=? "
                             "ORDER BY seq DESC LIMIT 1", (now.isoformat(),)).fetchone()
            epoch = db.execute("SELECT value FROM meta WHERE key='fault_epoch'").fetchone()[0]
            if row is None or row[4] != epoch:
                raise MarketEventError("共享行情缺失或有未恢复故障")
            close_at, received = _parse_time(row[0]), _parse_time(row[1])
            if not (close_at <= received <= now and now - close_at <= MAX_SIGNAL_AGE):
                raise MarketEventError("共享行情尚未收盘、延迟或过期")
            data = json.loads(row[3])
            if event_digest(close_at, data) != row[2]:
                raise MarketEventError("共享行情事件内容哈希不匹配")
            _validated_candles(data, close_at)
            return MarketEvent(row[2], now, close_at, data, received)
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise MarketEventError(str(exc)) from exc
        finally:
            db.close()

    def latest_close(self) -> datetime | None:
        db = self._connect(readonly=True)
        try:
            row = db.execute("SELECT decision_time_utc FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            return _parse_time(row[0]) if row else None
        finally:
            db.close()

    def export_jsonl(self, path: str | Path) -> int:
        output = Path(path)
        if output.exists():
            raise MarketEventError("回放文件已存在，禁止覆盖")
        db = self._connect(readonly=True)
        try:
            rows = db.execute("SELECT kind,observed_at_utc,payload_json FROM audit ORDER BY seq").fetchall()
        finally:
            db.close()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            for kind, observed, payload in rows:
                stream.write(json.dumps({"kind": kind, "observed_at_utc": observed,
                                         "payload": json.loads(payload)},
                                        ensure_ascii=False, sort_keys=True) + "\n")
        return len(rows)

    @classmethod
    def replay_jsonl(cls, source: str | Path, target: str | Path) -> "LiveMarketJournal":
        journal = cls(target)
        journal.create()
        with Path(source).open(encoding="utf-8") as stream:
            for line in stream:
                item = json.loads(line)
                if item["kind"] == "fault":
                    journal.fault(item["payload"]["reason"],
                                  detected_at=_parse_time(item["observed_at_utc"]))
                elif item["kind"] == "event":
                    payload = item["payload"]
                    if _parse_time(item["observed_at_utc"]) != _parse_time(payload["received_at_utc"]):
                        raise MarketEventError("回放行情接收时刻与审计记录不一致")
                    event = journal.ingest(decision_time=_parse_time(payload["decision_time_utc"]),
                                           received_at=_parse_time(payload["received_at_utc"]),
                                           candles=payload["candles"], replay=True)
                    if event.event_id != payload["event_id"]:
                        raise MarketEventError("回放事件 ID 与原始审计记录不一致")
                else:
                    raise MarketEventError("回放文件包含未知记录类型")
        return journal
