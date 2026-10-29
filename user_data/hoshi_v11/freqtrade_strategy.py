"""V1.1 实验策略的共同 Freqtrade 适配层；仅允许离线回测和 Dry Run。"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from freqtrade.persistence import Trade

_STRATEGY_DIR = str(Path(__file__).resolve().parents[1] / "strategies")
if _STRATEGY_DIR not in sys.path:
    sys.path.insert(0, _STRATEGY_DIR)
from HoshiSpotV1 import HoshiSpotV1  # noqa: E402 - frozen signal/exit implementation

from hoshi.risk import minimum_order_reason, validate_market, within_open_trade_limit
from hoshi_v11.risk import ClosedTrade, EquityInput, PositionMark, RiskEngine, RiskPolicy
from hoshi_v11.risk.equity import planned_stake
from hoshi_v11.market_events import HistoricalEventFeed
from hoshi_v11.live_events import LiveMarketJournal
from hoshi_v11.order_guard import is_installed


log = logging.getLogger(__name__)


class MarketDataFault(ValueError):
    pass


class HoshiV11Base(HoshiSpotV1):
    """A/B/C 共享适配层；任何风控故障只会拒绝入场，不拦截原有退出。"""

    account_id: str = "UNSET"
    risk_policy = RiskPolicy()
    risk_engine: RiskEngine | None = None
    last_risk_decision = None

    @property
    def protections(self) -> list[dict]:
        # The old 100000-candle MaxDrawdown is replaced by the persistent V1.1
        # engine. Native StoplossGuard remains an additional short entry lock.
        params = self._params()
        return [{
            "method": "StoplossGuard",
            "lookback_period_candles": int(params.get("stoploss_guard_lookback_candles", 24)),
            "trade_limit": int(params.get("stoploss_guard_trade_limit", 1)),
            "stop_duration_candles": int(params.get("stoploss_guard_stop_candles", 24)),
            "only_per_pair": False,
            "required_profit": 0.0,
        }]

    def bot_start(self, **kwargs) -> None:
        super().bot_start(**kwargs)
        runmode = getattr(self.config.get("runmode"), "value", self.config.get("runmode"))
        exchange = self.config.get("exchange", {})
        if (runmode not in {"backtest", "dry_run"} or self.config.get("dry_run") is not True
                or self.config.get("trading_mode") != "spot" or exchange.get("name") != "binance"
                or any(exchange.get(field) for field in
                       ("key", "secret", "password", "uid", "privateKey", "walletAddress"))
                or int(self.config.get("max_open_trades", -1)) != 1
                or self.config.get("stake_currency") != "USDT"
                or float(self.config.get("dry_run_wallet", 0)) != 80.0
                or self.account_id not in {"A", "B", "C"}):
            raise RuntimeError("Hoshi V1.1 安全启动检查失败：只允许无密钥 Binance 现货 80 USDT 模拟")
        if runmode == "backtest":
            run_id = os.environ.get("HOSHI_V11_RUN_ID", "")
            if not run_id or not run_id.replace("-", "").replace("_", "").isalnum():
                raise RuntimeError("离线回测必须提供安全的唯一 HOSHI_V11_RUN_ID")
            db = Path(__file__).resolve().parents[1] / "experiments" / "risk_state" / f"{self.account_id}-{run_id}.sqlite"
            if db.exists():
                raise RuntimeError("离线风控账本已存在；必须选择新 run_id，不得覆盖历史")
            self._risk_bootstrap_pending = True
            self.historical_feed = HistoricalEventFeed(
                Path(__file__).resolve().parents[1] / "data" / "binance"
            )
            self.market_feed = self.historical_feed
        else:
            if not is_installed():
                raise RuntimeError("V1.1 纸上执行订单隔离未安装，拒绝 Dry Run")
            db = Path(__file__).resolve().parents[1] / "v11_accounts" / self.account_id / "risk.sqlite"
            if not db.is_file():
                raise RuntimeError("Dry Run 风控账本缺失；拒绝自动重建")
            self._risk_bootstrap_pending = False
            self.market_feed = LiveMarketJournal(
                Path(__file__).resolve().parents[1] / "v11_market" / "shared.sqlite"
            )
            self.market_feed.at(datetime.now(timezone.utc))  # missing/stale feed rejects startup
        self.risk_engine = RiskEngine(account_id=self.account_id, db_path=db,
                                      policy=self.risk_policy)

    def _verify_shared_snapshot(self, event) -> None:
        """Dry Run 的 Freqtrade 指标输入必须与共享快照逐币逐周期一致。"""
        for pair in ("BTC/USDT", "ETH/USDT"):
            frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if frame is None or frame.empty:
                raise MarketDataFault(f"{pair} 缺少已分析行情")
            row = frame.iloc[-1]
            for timeframe, suffix, hours in (("1h", "", 1), ("4h", "_4h", 4)):
                date_key = "date" + suffix
                if date_key not in row:
                    raise MarketDataFault(f"{pair} 缺少共享快照时间列 {date_key}")
                candle_start = row[date_key].to_pydatetime()
                if candle_start.tzinfo is None:
                    candle_start = candle_start.replace(tzinfo=timezone.utc)
                actual_close = candle_start.astimezone(timezone.utc) + timedelta(hours=hours)
                expected = event.candles[pair][timeframe]
                if actual_close.isoformat() != expected["close_time_utc"]:
                    raise MarketDataFault(f"{pair} {timeframe} Freqtrade 与共享事件时间不一致")
                for field in ("open", "high", "low", "close", "volume"):
                    column = field + suffix
                    if column not in row or not math.isclose(
                        float(row[column]), float(expected[field]), rel_tol=1e-9, abs_tol=1e-8
                    ):
                        raise MarketDataFault(f"{pair} {timeframe} Freqtrade 与共享事件价格不一致")

    def _risk_inputs(self, current_time: datetime):
        try:
            event = self.market_feed.at(current_time)
        except ValueError as exc:
            raise MarketDataFault(str(exc)) from exc
        runmode = getattr(self.config.get("runmode"), "value", self.config.get("runmode"))
        if runmode == "dry_run":
            self._verify_shared_snapshot(event)
        closed = []
        for trade in Trade.get_trades_proxy(is_open=False):
            if trade.close_date_utc is None:
                continue
            closed.append(ClosedTrade(
                trade_id=str(trade.id), closed_at=trade.close_date_utc,
                net_profit_usdt=float(trade.close_profit_abs),
                exit_reason=str(trade.exit_reason or ""),
            ))
        positions = []
        for trade in Trade.get_trades_proxy(is_open=True):
            price = event.price(trade.pair)
            positions.append(PositionMark(
                pair=trade.pair, quantity=float(trade.amount), price=price,
                estimated_exit_fee_fraction=float(trade.fee_close or self.risk_policy.fee_per_side),
                entry_cost_usdt=float(trade.open_trade_value),
            ))
        closed_sum = sum(item.net_profit_usdt for item in closed)
        cash = 80.0 + closed_sum - sum(item.entry_cost_usdt for item in positions)
        # Compare the independent Freqtrade wallet with the durable trade math.
        # A pending/unknown simulated order is a reconciliation failure.
        wallet_cash = float(self.wallets.get_free("USDT"))
        if not math.isfinite(wallet_cash) or abs(wallet_cash - cash) > 0.05:
            raise ValueError(f"模拟钱包现金与交易记录不一致：wallet={wallet_cash:.6f}, ledger={cash:.6f}")
        return (EquityInput(cash, tuple(positions)), tuple(closed),
                event.event_id, event.one_hour_close_utc)

    def _assess(self, current_time: datetime):
        if self.risk_engine is None:
            raise RuntimeError("风控模块尚未初始化")
        if self._risk_bootstrap_pending:
            self.risk_engine.bootstrap(stage_id=f"{self.account_id}-initial", initial_equity=80,
                                       started_at=current_time)
            self._risk_bootstrap_pending = False
        try:
            equity, trades, event_id, candle_close = self._risk_inputs(current_time)
        except MarketDataFault as exc:
            decision = self.risk_engine.record_data_fault(now=current_time, reason=str(exc))
            self.last_risk_decision = decision
            return decision
        except Exception as exc:
            decision = self.risk_engine.record_emergency(now=current_time,
                                                         reason=f"模拟账户输入无法对账：{exc}")
            self.last_risk_decision = decision
            return decision
        self._last_market_event_id = event_id
        decision = self.risk_engine.evaluate(
            now=current_time, equity_input=equity, closed_trades=trades,
            market_event_id=event_id, last_closed_candle_at=candle_close,
            market_valid=True, account_reconciled=True,
            dry_run=self.config.get("dry_run") is True,
            trading_mode=self.config.get("trading_mode"),
            exchange_name=self.config.get("exchange", {}).get("name"),
            exchange_key=self.config.get("exchange", {}).get("key", ""),
            exchange_secret=self.config.get("exchange", {}).get("secret", ""),
        )
        self.last_risk_decision = decision
        return decision

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        try:
            decision = self._assess(current_time)
            if not decision.allow_new_entry:
                log.info("V1.1 %s 新开仓暂停：%s / %s", self.account_id,
                         decision.state, decision.rule)
        except Exception:
            log.exception("V1.1 %s 风控采样失败，拒绝新开仓", self.account_id)
            self.last_risk_decision = None

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: float | None,
                            max_stake: float, leverage: float, entry_tag: str | None,
                            side: str, **kwargs) -> float:
        if side != "long" or not math.isfinite(current_rate) or current_rate <= 0:
            return 0.0
        try:
            decision = self._assess(current_time)
        except Exception:
            log.exception("V1.1 %s 仓位前风控失败，拒绝 %s", self.account_id, pair)
            return 0.0
        if not decision.allow_new_entry:
            try:
                self.risk_engine.record_blocked_candidate(
                    now=current_time,
                    signal_id=hashlib.sha256(
                        f"{self.account_id}|{pair}|{current_time.isoformat()}".encode()
                    ).hexdigest(),
                    market_event_id=self._last_market_event_id,
                    decision=decision,
                )
            except Exception:
                log.exception("V1.1 %s 被锁信号记录失败，保持拒绝 %s", self.account_id, pair)
            return 0.0
        try:
            equity = float(self.wallets.get_total_stake_amount()) if self.wallets else 0.0
            stake = planned_stake(available_cash=max_stake, equity=equity,
                                  open_trades=Trade.get_open_trade_count(),
                                  policy=self.risk_policy, leverage=leverage,
                                  trading_mode=self.config.get("trading_mode", ""))
            market = self.dp.market(pair) if self.dp else None
            precision = (market or {}).get("precision", {}).get("amount")
            step = float(precision) if isinstance(precision, (float, int)) and 0 < precision < 1 else None
            if minimum_order_reason(stake, min_stake, amount_step=step, price=current_rate):
                return 0.0
            return stake
        except Exception:
            log.exception("V1.1 %s 仓位计算失败，拒绝 %s", self.account_id, pair)
            return 0.0

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time: datetime,
                            entry_tag: str | None, side: str, **kwargs) -> bool:
        # Never call V1's entry callback: its 100000-candle/stage rules are the
        # frozen baseline. Exit callbacks are inherited unchanged.
        try:
            runmode = getattr(self.config.get("runmode"), "value", self.config.get("runmode"))
            if (runmode not in {"backtest", "dry_run"} or self.config.get("dry_run") is not True
                    or side != "long" or pair not in {"BTC/USDT", "ETH/USDT"}
                    or not within_open_trade_limit(Trade.get_open_trade_count(), 1)):
                return False
            frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if frame is None or frame.empty:
                return False
            candle = frame.iloc[-1]
            candle_start = candle["date"].to_pydatetime()
            if candle_start.tzinfo is None:
                candle_start = candle_start.replace(tzinfo=timezone.utc)
            candle_close = candle_start + timedelta(hours=1)
            if validate_market(
                candle_time=candle_close, current_time=current_time,
                reference_price=float(candle["close"]), current_price=float(rate),
                candle_change=float(candle["candle_change"]),
                signal_created_at=candle_close, settings=self._risk_settings(),
            ):
                return False
            if not self.event_file.exists():
                return False
            from hoshi.risk import event_blackout_reason, read_event_blackouts
            if event_blackout_reason(current_time, read_event_blackouts(self.event_file)):
                return False
            decision = self._assess(current_time)
            if not decision.allow_new_entry:
                if self.risk_engine is not None:
                    self.risk_engine.record_blocked_candidate(
                        now=current_time,
                        signal_id=hashlib.sha256(f"{self.account_id}|{pair}|{candle_close.isoformat()}".encode()).hexdigest(),
                        market_event_id=self._last_market_event_id,
                        decision=decision,
                    )
                return False
            notional = float(amount) * float(rate)
            if not math.isfinite(notional) or notional <= 0:
                return False
            permitted = planned_stake(
                available_cash=float(self.wallets.get_free("USDT")),
                equity=float(decision.details["equity"]), open_trades=Trade.get_open_trade_count(),
                policy=self.risk_policy, leverage=1.0,
                trading_mode=self.config.get("trading_mode", ""),
            )
            if notional > permitted + 0.02:
                log.error("V1.1 %s 拒绝超额模拟仓位：%.4f > %.4f", self.account_id,
                          notional, permitted)
                return False
            return True
        except Exception:
            log.exception("V1.1 %s 入场闸门失败，拒绝 %s", self.account_id, pair)
            return False
