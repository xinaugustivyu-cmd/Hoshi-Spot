"""可重放的入场闸门；任何风险状态均保留已有模拟仓位的退出权限。"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .equity import simulated_equity
from .models import ClosedTrade, EquityInput, RiskDecision, RiskPolicy
from .state_store import RiskStateError, StateStore, iso


STOP_REASONS = {"stop_loss", "stoploss", "stoploss_on_exchange", "trailing_stop_loss"}
log = logging.getLogger(__name__)


class ApprovalDenied(RiskStateError):
    """人工复核尚未满足恢复条件；原有长期停机继续生效。"""


def monday_start(now: datetime) -> datetime:
    value = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return value - timedelta(days=value.weekday())


def next_day(now: datetime) -> datetime:
    value = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return value + timedelta(days=1)


def _finite_positive(value: float) -> bool:
    return math.isfinite(value) and value > 0


class RiskEngine:
    def __init__(self, *, account_id: str, db_path: str | Path,
                 policy: RiskPolicy = RiskPolicy()):
        if not account_id or "/" in account_id or "\\" in account_id:
            raise ValueError("模拟账户 ID 无效")
        self.account_id = account_id
        self.store = StateStore(db_path)
        self.policy = policy

    def bootstrap(self, *, stage_id: str, initial_equity: float, started_at: datetime) -> None:
        """显式建账。正常启动永远不会隐式创建或清空账本。"""
        if initial_equity != 80.0:
            raise ValueError("V1.1 初始虚拟本金必须为 80 USDT")
        self.store.bootstrap(account_id=self.account_id, stage_id=stage_id,
                             initial_equity=initial_equity, started_at=started_at,
                             policy_hash=self.policy.hash)

    def record_data_fault(self, *, now: datetime, reason: str) -> RiskDecision:
        """行情缺失时无需伪造估值，仍要持久化两根新 K 线的恢复门槛。"""
        now = datetime.fromisoformat(iso(now))
        try:
            connection = self.store.connect(account_id=self.account_id,
                                            policy_hash=self.policy.hash)
            try:
                with connection:
                    self.store.begin_write(connection)
                    self.store.set_meta(connection, "data_pause", True)
                    self.store.set_meta(connection, "healthy_candles", 0)
                    last = self.store.get_meta(connection, "last_closed_candle_at")
                    if not last or now > datetime.fromisoformat(last):
                        self.store.set_meta(connection, "last_closed_candle_at", iso(now))
                    self._audit(connection, now, "DATA_PAUSE", "invalid_market_data", {"reason": reason})
                    halt = self.store.get_meta(connection, "manual_halt")
                    state = ("EMERGENCY_HALT" if halt.startswith("EMERGENCY:") else
                             "HALTED_MANUAL_REVIEW" if halt else "DATA_PAUSE")
                    return self._decision(connection, now, state, "invalid_market_data", None,
                                          {"reason": reason, "healthy_candles": 0})
            finally:
                connection.close()
        except (sqlite3.DatabaseError, RiskStateError, OSError, ValueError) as exc:
            return self._emergency(now, f"数据故障无法持久化：{exc}")

    def record_emergency(self, *, now: datetime, reason: str) -> RiskDecision:
        now = datetime.fromisoformat(iso(now))
        self._latch_emergency(now, reason)
        return self._emergency(now, reason)

    def _emergency(self, now: datetime, reason: str) -> RiskDecision:
        # A damaged/missing database may prevent durable recording. Returning a hard
        # deny is still mandatory. The sidecar keeps that denial after restart.
        marker = self.store.path.with_suffix(self.store.path.suffix + ".emergency.json")
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            if not marker.exists():
                temporary = marker.with_name(marker.name + ".tmp")
                temporary.write_text(json.dumps({
                    "account_id": self.account_id, "at": iso(now), "reason": reason,
                    "policy_hash": self.policy.hash,
                }, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, marker)
        except (OSError, ValueError):
            pass
        return RiskDecision(self.account_id, "EMERGENCY_HALT", "unpersisted-emergency",
                            now, reason, {"error": reason}, None, None, False, True,
                            False, self.policy.version, self.policy.hash)

    def _audit(self, connection: sqlite3.Connection, now: datetime, state: str,
               rule: str, details: dict) -> str:
        event_id = str(uuid4())
        connection.execute(
            "INSERT INTO risk_events(event_id, observed_at, state, rule, details_json, policy_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, iso(now), state, rule,
             json.dumps(details, ensure_ascii=False, sort_keys=True), self.policy.hash),
        )
        return event_id

    def _sync_trades(self, connection: sqlite3.Connection, now: datetime,
                     closed_trades: tuple[ClosedTrade, ...]) -> None:
        stored = {
            row[0]: row[1:] for row in connection.execute(
                "SELECT trade_id, closed_at, net_profit_usdt, exit_reason FROM closed_trades"
            )
        }
        observed: dict[str, tuple[str, float, str]] = {}
        for trade in closed_trades:
            if not trade.trade_id or not math.isfinite(trade.net_profit_usdt):
                raise RiskStateError("平仓交易 ID 或净损益无效")
            close_at = iso(trade.closed_at)
            if trade.closed_at.astimezone(timezone.utc) > now:
                raise RiskStateError("平仓时间位于未来")
            value = (close_at, trade.net_profit_usdt, trade.exit_reason)
            if trade.trade_id in observed and observed[trade.trade_id] != value:
                raise RiskStateError("同一交易 ID 有不同结果")
            observed[trade.trade_id] = value
        if not stored.keys() <= observed.keys():
            raise RiskStateError("模拟交易历史缺失，无法跨阶段对账")
        stage_id = self.store.get_meta(connection, "stage_id")
        for trade_id, value in observed.items():
            if trade_id in stored:
                if stored[trade_id] != value:
                    raise RiskStateError("模拟交易历史与风险账本不一致")
            else:
                connection.execute(
                    "INSERT INTO closed_trades VALUES (?, ?, ?, ?, ?)",
                    (trade_id, stage_id, *value),
                )
                self._audit(connection, now, "ACTIVE", "trade_closed", {
                    "trade_id": trade_id, "stage_id": stage_id,
                    "close_at": value[0], "net_profit_usdt": value[1],
                    "exit_reason": value[2],
                })

    def _week_opening(self, connection: sqlite3.Connection, now: datetime) -> float:
        week = monday_start(now)
        row = connection.execute(
            "SELECT opening_equity FROM week_snapshots WHERE week_start=?", (iso(week),)
        ).fetchone()
        if row is not None:
            return float(row[0])
        previous = connection.execute(
            "SELECT observed_at, equity FROM equity_marks ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if previous is None:
            raise RiskStateError("周初权益快照缺失")
        previous_at = datetime.fromisoformat(previous[0])
        if not (previous_at <= week <= now and now - previous_at <= timedelta(minutes=75)):
            raise RiskStateError("跨周行情中断，无法确认 UTC 周初权益")
        opening = float(previous[1])
        connection.execute(
            "INSERT INTO week_snapshots VALUES (?, ?, ?)",
            (iso(week), opening, iso(now)),
        )
        self._audit(connection, now, "ACTIVE", "week_opening_snapshot", {
            "week_start": iso(week), "opening_equity": opening,
        })
        return opening

    def evaluate(
        self, *, now: datetime, equity_input: EquityInput,
        closed_trades: tuple[ClosedTrade, ...], market_event_id: str,
        last_closed_candle_at: datetime | None, source: str = "candle_close",
        market_valid: bool = True, account_reconciled: bool = True,
        dry_run: bool = True, trading_mode: str = "spot",
        exchange_name: str = "binance", leverage: float = 1.0,
        exchange_key: str = "", exchange_secret: str = "",
    ) -> RiskDecision:
        try:
            now = datetime.fromisoformat(iso(now))
        except (TypeError, ValueError) as exc:
            return self._emergency(datetime.now(timezone.utc), f"时间异常：{exc}")
        marker = self.store.path.with_suffix(self.store.path.suffix + ".emergency.json")
        if marker.exists():
            return self._emergency(now, "风险账本曾发生未完成的紧急故障，需人工修复")
        try:
            connection = self.store.connect(account_id=self.account_id,
                                            policy_hash=self.policy.hash)
        except (sqlite3.DatabaseError, RiskStateError, OSError, ValueError) as exc:
            return self._emergency(now, f"风险账本不可用：{exc}")
        try:
            with connection:
                self.store.begin_write(connection)
                return self._evaluate_locked(
                    connection, now=now, equity_input=equity_input,
                    closed_trades=closed_trades, market_event_id=market_event_id,
                    last_closed_candle_at=last_closed_candle_at,
                    source=source, market_valid=market_valid,
                    account_reconciled=account_reconciled, dry_run=dry_run,
                    trading_mode=trading_mode, exchange_name=exchange_name,
                    leverage=leverage, exchange_key=exchange_key,
                    exchange_secret=exchange_secret,
                )
        except (sqlite3.DatabaseError, RiskStateError, ValueError, TypeError, OverflowError) as exc:
            log.exception("V1.1 %s 风控写入事务失败", self.account_id)
            # Release any transaction or OS lock before the independent halt
            # transaction. A failed commit may still leave the handle open.
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            try:
                connection.close()
            except sqlite3.Error:
                pass
            self._latch_emergency(now, f"风控评估失败：{exc}")
            return self._emergency(now, f"风控评估失败：{exc}")
        finally:
            connection.close()

    def _latch_emergency(self, now: datetime, reason: str) -> None:
        """事务回滚后单独落盘；账本本身损坏时仍保持拒绝开仓。"""
        try:
            connection = self.store.connect(account_id=self.account_id,
                                            policy_hash=self.policy.hash)
            try:
                with connection:
                    self.store.begin_write(connection)
                    self.store.set_meta(connection, "manual_halt", f"EMERGENCY:{reason}")
                    self.store.set_meta(connection, "manual_halt_at", iso(now))
                    self._audit(connection, now, "EMERGENCY_HALT", "emergency", {"reason": reason})
            finally:
                connection.close()
        except (sqlite3.DatabaseError, RiskStateError, OSError, ValueError):
            pass

    def _evaluate_locked(self, connection: sqlite3.Connection, *, now: datetime,
                         equity_input: EquityInput, closed_trades: tuple[ClosedTrade, ...],
                         market_event_id: str, last_closed_candle_at: datetime | None,
                         source: str, market_valid: bool, account_reconciled: bool,
                         dry_run: bool, trading_mode: str, exchange_name: str,
                         leverage: float, exchange_key: str, exchange_secret: str) -> RiskDecision:
        safety_ok = (dry_run is True and trading_mode == "spot" and exchange_name == "binance"
                     and leverage == 1.0 and not exchange_key and not exchange_secret)
        emergency = None
        if not safety_ok:
            emergency = "模拟权限、交易模式或交易所配置异常"
        elif not account_reconciled:
            emergency = "模拟钱包、订单或持仓无法对账"
        try:
            equity = simulated_equity(equity_input)
        except ValueError as exc:
            equity = None
            emergency = f"模拟权益无法确认：{exc}"
        if equity is not None:
            expected_cash = (80.0 + sum(trade.net_profit_usdt for trade in closed_trades)
                             - sum(position.entry_cost_usdt for position in equity_input.positions))
            if abs(equity_input.cash_usdt - expected_cash) > 0.02:
                emergency = "模拟现金与累计交易净损益及未平仓成本不一致"
        previous_at_text = self.store.get_meta(connection, "last_observed_at")
        if previous_at_text and now < datetime.fromisoformat(previous_at_text):
            emergency = "风险评估时间倒退"
        if emergency:
            old_halt = self.store.get_meta(connection, "manual_halt")
            if not old_halt.startswith("EMERGENCY:"):
                self.store.set_meta(connection, "manual_halt", f"EMERGENCY:{emergency}")
                self.store.set_meta(connection, "manual_halt_at", iso(now))
                self._audit(connection, now, "EMERGENCY_HALT", "emergency", {"reason": emergency})
            return self._decision(connection, now, "EMERGENCY_HALT", emergency, None,
                                  {"reason": emergency})

        self._sync_trades(connection, now, closed_trades)
        week_opening = self._week_opening(connection, now)
        if not _finite_positive(week_opening):
            raise RiskStateError("周初权益无效")
        valid_candle = False
        if last_closed_candle_at is not None:
            candle_at = datetime.fromisoformat(iso(last_closed_candle_at))
            valid_candle = (candle_at <= now and now - candle_at <= timedelta(minutes=75))
        else:
            candle_at = None
        market_ok = (market_valid and valid_candle and bool(market_event_id)
                     and source in {"candle_close", "public_quote"})
        last_candle_text = self.store.get_meta(connection, "last_closed_candle_at")
        last_event_id = self.store.get_meta(connection, "last_market_event_id")
        if candle_at is not None and last_candle_text and candle_at < datetime.fromisoformat(last_candle_text):
            market_ok = False
        if (source == "candle_close" and candle_at is not None and last_candle_text
                and candle_at == datetime.fromisoformat(last_candle_text)
                and last_event_id and market_event_id != last_event_id):
            market_ok = False
        data_pause = bool(self.store.get_meta(connection, "data_pause"))
        healthy_candles = int(self.store.get_meta(connection, "healthy_candles"))
        if not market_ok:
            if not data_pause:
                self._audit(connection, now, "DATA_PAUSE", "invalid_market_data", {
                    "market_event_id": market_event_id, "source": source,
                })
            # A faulty candle cannot count as either of the two *new* healthy
            # candles required for recovery.
            if (candle_at is not None and candle_at <= now
                    and (not last_candle_text or candle_at > datetime.fromisoformat(last_candle_text))):
                self.store.set_meta(connection, "last_closed_candle_at", iso(candle_at))
                self.store.set_meta(connection, "last_market_event_id", market_event_id)
            data_pause, healthy_candles = True, 0
        elif candle_at is not None and (not last_candle_text or candle_at > datetime.fromisoformat(last_candle_text)):
            self.store.set_meta(connection, "last_closed_candle_at", iso(candle_at))
            self.store.set_meta(connection, "last_market_event_id", market_event_id)
            if data_pause:
                healthy_candles += 1
                if healthy_candles >= 2:
                    data_pause, healthy_candles = False, 0
        self.store.set_meta(connection, "data_pause", data_pause)
        self.store.set_meta(connection, "healthy_candles", healthy_candles)

        peak = float(self.store.get_meta(connection, "stage_peak_equity"))
        if market_ok:
            peak = max(peak, equity)
            self.store.set_meta(connection, "stage_peak_equity", peak)
            drawdown = (peak - equity) / peak
            connection.execute(
                "INSERT INTO equity_marks(observed_at, market_event_id, source, equity, stage_peak, stage_drawdown) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (iso(now), market_event_id, source, equity, peak, drawdown),
            )
        else:
            drawdown = None

        start_14d = now - timedelta(days=14)
        trades = connection.execute(
            "SELECT closed_at, net_profit_usdt, exit_reason FROM closed_trades WHERE closed_at <= ?",
            (iso(now),),
        ).fetchall()
        rolling_pnl = sum(float(pnl) for date, pnl, _ in trades if datetime.fromisoformat(date) > start_14d)
        week_pnl = sum(float(pnl) for date, pnl, _ in trades if datetime.fromisoformat(date) >= monday_start(now))
        stage_initial = float(self.store.get_meta(connection, "stage_initial_equity"))
        reasons = []
        if rolling_pnl <= -stage_initial * self.policy.rolling_14d_loss_fraction:
            reasons.append("rolling_14d_loss")
        if drawdown is not None and drawdown >= self.policy.max_drawdown_fraction:
            reasons.append("max_drawdown")
        old_halt = self.store.get_meta(connection, "manual_halt")
        new_reasons = [reason for reason in reasons if reason not in old_halt.split(",")]
        if new_reasons and not old_halt.startswith("EMERGENCY:"):
            combined = ",".join(filter(None, [old_halt, *new_reasons]))
            self.store.set_meta(connection, "manual_halt", combined)
            if not old_halt:
                self.store.set_meta(connection, "manual_halt_at", iso(now))
            self._audit(connection, now, "HALTED_MANUAL_REVIEW", ",".join(new_reasons), {
                "rolling_14d_net": rolling_pnl, "drawdown": drawdown,
                "stage_initial_equity": stage_initial, "equity": equity, "peak": peak,
            })
        self.store.set_meta(connection, "last_observed_at", iso(now))
        details = {"equity": equity, "stage_peak": peak, "drawdown": drawdown,
                   "rolling_14d_net": rolling_pnl, "week_net": week_pnl,
                   "week_opening_equity": week_opening,
                   "stage_id": self.store.get_meta(connection, "stage_id")}
        manual_halt = self.store.get_meta(connection, "manual_halt")
        if manual_halt.startswith("EMERGENCY:"):
            return self._decision(connection, now, "EMERGENCY_HALT", manual_halt, None, details)
        if manual_halt:
            return self._decision(connection, now, "HALTED_MANUAL_REVIEW", manual_halt, None, details)
        if week_pnl <= -week_opening * self.policy.weekly_loss_fraction:
            return self._decision(connection, now, "WEEKLY_PAUSE", "weekly_loss",
                                  monday_start(now) + timedelta(days=7), details)
        if data_pause:
            return self._decision(connection, now, "DATA_PAUSE", "invalid_market_data", None,
                                  {**details, "healthy_candles": healthy_candles})
        cool_until = None
        for date, pnl, reason in trades:
            closed_at = datetime.fromisoformat(date)
            if pnl < 0 and reason.lower() in STOP_REASONS:
                candidate = max(closed_at + timedelta(hours=24), next_day(closed_at))
                if candidate > now and (cool_until is None or candidate > cool_until):
                    cool_until = candidate
        if cool_until is not None:
            return self._decision(connection, now, "COOLDOWN", "stoploss_cooldown",
                                  cool_until, details)
        return self._decision(connection, now, "ACTIVE", "checks_passed", None, details)

    def _decision(self, connection: sqlite3.Connection, now: datetime, state: str,
                  rule: str, pause_until: datetime | None, details: dict) -> RiskDecision:
        previous = self.store.get_meta(connection, "last_state")
        state_id = self.store.get_meta(connection, "last_state_id")
        if state != previous:
            audit_details = {**details, "pause_until": iso(pause_until) if pause_until else None,
                             "auto_resume": state in {"COOLDOWN", "WEEKLY_PAUSE", "DATA_PAUSE"},
                             "allow_new_entry": state == "ACTIVE"}
            state_id = self._audit(connection, now, state, rule, audit_details)
            self.store.set_meta(connection, "last_state", state)
            self.store.set_meta(connection, "last_state_id", state_id)
            self.store.set_meta(connection, "last_state_at", iso(now))
        last_approval = connection.execute(
            "SELECT approval_id FROM approvals ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return RiskDecision(
            account_id=self.account_id, state=state, state_id=state_id,
            triggered_at=datetime.fromisoformat(self.store.get_meta(connection, "last_state_at")),
            rule=rule, details=details, pause_until=pause_until,
            approval_id=last_approval[0] if last_approval else None,
            allow_new_entry=state == "ACTIVE", allow_exit=True,
            auto_resume=state in {"COOLDOWN", "WEEKLY_PAUSE", "DATA_PAUSE"},
            policy_version=self.policy.version, policy_hash=self.policy.hash,
        )

    def resume_after_review(self, *, approval_id: str, reviewer: str, reason: str,
                            new_stage_id: str, now: datetime, equity_input: EquityInput,
                            closed_trades: tuple[ClosedTrade, ...],
                            account_reconciled: bool = True) -> RiskDecision:
        """只恢复模拟研究；14 日亏损仍超限时拒绝。不会删历史记录。"""
        now = datetime.fromisoformat(iso(now))
        if not all((approval_id, reviewer, reason, new_stage_id)) or not account_reconciled:
            return self._emergency(now, "人工审批或模拟账户对账不完整")
        marker = self.store.path.with_suffix(self.store.path.suffix + ".emergency.json")
        clear_marker = False
        try:
            equity = simulated_equity(equity_input)
            expected_cash = (80.0 + sum(trade.net_profit_usdt for trade in closed_trades)
                             - sum(position.entry_cost_usdt for position in equity_input.positions))
            if abs(equity_input.cash_usdt - expected_cash) > 0.02:
                raise ValueError("人工确认权益与交易账本不一致")
            connection = self.store.connect(account_id=self.account_id,
                                            policy_hash=self.policy.hash)
        except (ValueError, sqlite3.DatabaseError, RiskStateError, OSError) as exc:
            return self._emergency(now, f"人工恢复前校验失败：{exc}")
        try:
            with connection:
                self.store.begin_write(connection)
                self._sync_trades(connection, now, closed_trades)
                current_halt = self.store.get_meta(connection, "manual_halt")
                if marker.exists() and not current_halt:
                    current_halt = "EMERGENCY:unpersisted-marker"
                if not current_halt:
                    raise ApprovalDenied("账户没有需要人工恢复的长期停机")
                if connection.execute("SELECT 1 FROM approvals WHERE approval_id=?", (approval_id,)).fetchone():
                    raise ApprovalDenied("审批 ID 重复")
                previous_stage = self.store.get_meta(connection, "stage_id")
                if new_stage_id == previous_stage or connection.execute(
                    "SELECT 1 FROM approvals WHERE new_stage=?", (new_stage_id,)
                ).fetchone():
                    raise ApprovalDenied("阶段 ID 必须唯一")
                latest = connection.execute(
                    "SELECT observed_at, equity FROM equity_marks ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if latest is None or now - datetime.fromisoformat(latest[0]) > timedelta(minutes=75):
                    raise ApprovalDenied("缺少新鲜模拟权益估值")
                if abs(float(latest[1]) - equity) > 1e-7:
                    raise ApprovalDenied("人工确认权益与最近估值不一致")
                if self.store.get_meta(connection, "data_pause"):
                    raise ApprovalDenied("数据故障暂停仍未解除")
                start_14d = iso(now - timedelta(days=14))
                rolling = connection.execute(
                    "SELECT COALESCE(SUM(net_profit_usdt), 0) FROM closed_trades "
                    "WHERE closed_at > ? AND closed_at <= ?", (start_14d, iso(now))
                ).fetchone()[0]
                old_initial = float(self.store.get_meta(connection, "stage_initial_equity"))
                if rolling <= -old_initial * self.policy.rolling_14d_loss_fraction:
                    raise ApprovalDenied("滚动 14 日亏损仍达到 5%，禁止人工恢复")
                if not _finite_positive(equity):
                    raise ApprovalDenied("新阶段权益无效")
                connection.execute(
                    "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (approval_id, iso(now), reviewer, reason, previous_stage, new_stage_id, equity),
                )
                self.store.set_meta(connection, "stage_id", new_stage_id)
                self.store.set_meta(connection, "stage_initial_equity", equity)
                self.store.set_meta(connection, "stage_peak_equity", equity)
                self.store.set_meta(connection, "manual_halt", "")
                self.store.set_meta(connection, "manual_halt_at", "")
                clear_marker = True
                self._audit(connection, now, "ACTIVE", "manual_resume", {
                    "approval_id": approval_id, "reviewer": reviewer,
                    "previous_stage": previous_stage, "new_stage": new_stage_id,
                    "equity": equity, "lifetime_closed_net": connection.execute(
                        "SELECT COALESCE(SUM(net_profit_usdt), 0) FROM closed_trades"
                    ).fetchone()[0],
                })
                week_opening = self._week_opening(connection, now)
                week_pnl = connection.execute(
                    "SELECT COALESCE(SUM(net_profit_usdt), 0) FROM closed_trades "
                    "WHERE closed_at >= ? AND closed_at <= ?",
                    (iso(monday_start(now)), iso(now)),
                ).fetchone()[0]
                if week_pnl <= -week_opening * self.policy.weekly_loss_fraction:
                    return self._decision(connection, now, "WEEKLY_PAUSE", "weekly_loss",
                                          monday_start(now) + timedelta(days=7),
                                          {"new_stage": new_stage_id, "equity": equity})
                for date, pnl, exit_reason in connection.execute(
                    "SELECT closed_at, net_profit_usdt, exit_reason FROM closed_trades"
                ):
                    closed_at = datetime.fromisoformat(date)
                    if (pnl < 0 and exit_reason.lower() in STOP_REASONS
                            and max(closed_at + timedelta(hours=24), next_day(closed_at)) > now):
                        return self._decision(connection, now, "COOLDOWN", "stoploss_cooldown",
                                              max(closed_at + timedelta(hours=24), next_day(closed_at)),
                                              {"new_stage": new_stage_id, "equity": equity})
                return self._decision(connection, now, "ACTIVE", "manual_resume", None,
                                      {"new_stage": new_stage_id, "equity": equity})
        except ApprovalDenied as exc:
            try:
                with connection:
                    self.store.begin_write(connection)
                    self._audit(connection, now, "HALTED_MANUAL_REVIEW", "approval_rejected", {
                        "approval_id": approval_id, "reason": str(exc),
                    })
                    halt = self.store.get_meta(connection, "manual_halt")
                    state = "EMERGENCY_HALT" if halt.startswith("EMERGENCY:") else "HALTED_MANUAL_REVIEW"
                    return self._decision(connection, now, state, "approval_rejected", None,
                                          {"reason": str(exc)})
            except (sqlite3.DatabaseError, RiskStateError, ValueError) as audit_exc:
                return self._emergency(now, f"人工恢复拒绝记录失败：{audit_exc}")
        except (sqlite3.DatabaseError, RiskStateError, ValueError) as exc:
            return self._emergency(now, f"人工恢复被拒绝：{exc}")
        finally:
            connection.close()
            if clear_marker:
                try:
                    marker.unlink(missing_ok=True)
                except OSError:
                    # A failed marker removal still keeps all future entries denied.
                    pass

    def record_blocked_candidate(self, *, now: datetime, signal_id: str,
                                 market_event_id: str, decision: RiskDecision) -> None:
        if decision.allow_new_entry or not signal_id or not market_event_id:
            raise ValueError("只能记录被拦截的完整候选信号")
        connection = self.store.connect(account_id=self.account_id,
                                        policy_hash=self.policy.hash)
        try:
            with connection:
                self.store.begin_write(connection)
                self._audit(connection, now, decision.state, "blocked_candidate", {
                    "signal_id": signal_id, "market_event_id": market_event_id,
                    "state_id": decision.state_id, "rule": decision.rule,
                })
        finally:
            connection.close()
