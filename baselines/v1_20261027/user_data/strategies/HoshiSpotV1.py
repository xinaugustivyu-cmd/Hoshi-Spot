from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pandas import DataFrame
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative

# Freqtrade resolves strategy files from user_data/strategies; add its parent so
# the sibling `hoshi` package is importable in the official container image.
_USER_DATA = str(Path(__file__).resolve().parents[1])
if _USER_DATA not in sys.path:
    sys.path.insert(0, _USER_DATA)

from hoshi.advisor import explain_candidate
from hoshi.indicators import add_1h_indicators, add_4h_indicators, entry_mask, exit_mask
from hoshi.risk import (
    RiskSettings,
    TradeResult,
    event_blackout_reason,
    loss_limit_reason,
    minimum_order_reason,
    planned_stake,
    read_event_blackouts,
    validate_market,
    within_open_trade_limit,
)

logger = logging.getLogger(__name__)


class HoshiSpotV1(IStrategy):
    """只用于 Binance 现货研究、Freqtrade 回测和 Dry Run 的 Hoshi V1。"""

    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    startup_candle_count = 250
    process_only_new_candles = True
    use_exit_signal = True
    position_adjustment_enable = False
    max_entry_position_adjustment = 0

    # Freqtrade strategy attributes remain conservative fallbacks; config.json is authoritative.
    minimal_roi = {"0": 0.06}
    stoploss = -0.04

    def bot_start(self, **kwargs) -> None:
        runmode = getattr(self.config.get("runmode"), "value", self.config.get("runmode"))
        if runmode not in {"dry_run", "backtest", "plot", "hyperopt", "util_no_exchange"}:
            raise RuntimeError("HoshiSpotV1 仅允许 dry_run/backtest/离线分析工具；实盘模式已拒绝")
        if runmode == "dry_run" and self.config.get("dry_run") is not True:
            raise RuntimeError("安全约束拒绝启动：dry_run 必须为 true")
        self.params = dict(self.config.get("strategy_parameters", {}))
        self.settings = RiskSettings(
            max_position_fraction=float(self.params.get("max_position_fraction", 0.20)),
            max_loss_fraction=float(self.params.get("max_loss_fraction", 0.01)),
            stoploss_fraction=float(self.params.get("stoploss_fraction", 0.04)),
            fee_per_side=float(self.params.get("fee_per_side", 0.001)),
            slippage_per_side=float(self.params.get("slippage_per_side", 0.0005)),
            max_signal_age_minutes=int(self.params.get("max_signal_age_minutes", 15)),
            max_entry_deviation=float(self.params.get("max_entry_deviation", 0.003)),
            max_candle_change=float(self.params.get("max_candle_change", 0.10)),
            weekly_loss_limit=float(self.params.get("weekly_loss_limit", 0.03)),
            stage_loss_limit_fraction=float(self.params.get("stage_loss_limit_fraction", 0.05)),
        )
        self.event_file = Path(__file__).resolve().parents[1] / "hoshi" / "event_blackouts.csv"
        self.suggestion_log = Path(__file__).resolve().parents[1] / "logs" / "suggestions.jsonl"

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return add_4h_indicators(dataframe, self._params())

    def _params(self) -> dict:
        return getattr(self, "params", dict(self.config.get("strategy_parameters", {})))

    @property
    def protections(self) -> list[dict]:
        """Freqtrade protections are strategy properties in current stable releases."""
        params = self._params()
        return [
            {
                "method": "MaxDrawdown",
                "calculation_mode": "equity",
                "lookback_period_candles": int(params.get("max_drawdown_lookback_candles", 100000)),
                "trade_limit": 1,
                "stop_duration_candles": int(params.get("max_drawdown_stop_candles", 100000)),
                "max_allowed_drawdown": float(params.get("max_drawdown_limit", 0.05)),
            },
            {
                "method": "StoplossGuard",
                "lookback_period_candles": int(params.get("stoploss_guard_lookback_candles", 24)),
                "trade_limit": int(params.get("stoploss_guard_trade_limit", 1)),
                "stop_duration_candles": int(params.get("stoploss_guard_stop_candles", 24)),
                "only_per_pair": False,
                "required_profit": 0.0,
            },
        ]

    def informative_pairs(self) -> list[tuple[str, str]]:
        # SOL 只获取公开行情用于观察，不加入可交易白名单，也不生成入场信号。
        return [("SOL/USDT", "1h"), ("SOL/USDT", "4h")]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return add_1h_indicators(dataframe, self._params())

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        result = dataframe.copy()
        result["enter_long"] = 0
        result["enter_tag"] = None
        mask = entry_mask(result, self._params())
        result.loc[mask, "enter_long"] = 1
        result.loc[mask, "enter_tag"] = "hoshi_v1_ema_rsi_volume"
        return result

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        result = dataframe.copy()
        result["exit_long"] = 0
        result["exit_tag"] = None
        mask = exit_mask(result)
        result.loc[mask, "exit_long"] = 1
        result.loc[mask, "exit_tag"] = "trend_invalid_close_below_ema20_rsi_lt_45"
        return result

    def custom_stake_amount(
        self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float,
        min_stake: float | None, max_stake: float, leverage: float, entry_tag: str | None,
        side: str, **kwargs,
    ) -> float:
        if side != "long" or leverage != 1:
            return 0.0
        equity = self.wallets.get_total_stake_amount() if self.wallets else max_stake
        stake = planned_stake(max_stake, equity, self._risk_settings())
        market = self.dp.market(pair) if self.dp else None
        precision = (market or {}).get("precision", {}).get("amount")
        # Binance's amount precision is a tick size; other precision modes remain
        # validated by Freqtrade's native amount_to_precision and market limits.
        amount_step = float(precision) if isinstance(precision, (float, int)) and 0 < precision < 1 else None
        reason = minimum_order_reason(stake, min_stake, amount_step=amount_step, price=current_rate)
        if reason:
            logger.info("Hoshi 风控拒绝 %s: %s", pair, reason)
            return 0.0
        return stake

    def _risk_settings(self) -> RiskSettings:
        if hasattr(self, "settings"):
            return self.settings
        params = self._params()
        return RiskSettings(
            max_position_fraction=float(params.get("max_position_fraction", 0.20)),
            max_loss_fraction=float(params.get("max_loss_fraction", 0.01)),
            stoploss_fraction=float(params.get("stoploss_fraction", 0.04)),
            fee_per_side=float(params.get("fee_per_side", 0.001)),
            slippage_per_side=float(params.get("slippage_per_side", 0.0005)),
            max_signal_age_minutes=int(params.get("max_signal_age_minutes", 15)),
            max_entry_deviation=float(params.get("max_entry_deviation", 0.003)),
            max_candle_change=float(params.get("max_candle_change", 0.10)),
            weekly_loss_limit=float(params.get("weekly_loss_limit", 0.03)),
            stage_loss_limit_fraction=float(params.get("stage_loss_limit_fraction", 0.05)),
        )

    def confirm_trade_entry(
        self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str,
        current_time: datetime, entry_tag: str | None, side: str, **kwargs,
    ) -> bool:
        """最后开仓闸门；任何非模拟模式、数据异常或风控错误都会拒绝新开仓。"""
        runmode = getattr(self.config.get("runmode"), "value", self.config.get("runmode"))
        if runmode not in {"dry_run", "backtest", "util_no_exchange"} or self.config.get("dry_run") is not True or side != "long":
            logger.error("安全闸门拒绝开仓：仅允许 dry_run 现货多头")
            return False
        try:
            frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if frame is None or len(frame) < 1:
                logger.warning("安全闸门拒绝 %s：缺少已分析行情", pair)
                return False
            candle = frame.iloc[-1]
            candle_start = candle["date"].to_pydatetime()
            if candle_start.tzinfo is None:
                candle_start = candle_start.replace(tzinfo=timezone.utc)
            candle_close_time = candle_start + timedelta(hours=1)
            reason = validate_market(
                candle_time=candle_close_time,
                current_time=current_time,
                reference_price=float(candle["close"]),
                current_price=float(rate),
                candle_change=float(candle["candle_change"]),
                signal_created_at=candle_close_time,
                settings=self._risk_settings(),
            )
            if reason:
                logger.info("安全闸门拒绝 %s：%s", pair, reason)
                return False
            if not self.event_file.exists():
                logger.error("安全闸门拒绝 %s：事件日历文件缺失", pair)
                return False
            if event_blackout_reason(current_time, read_event_blackouts(self.event_file)):
                logger.info("安全闸门拒绝 %s：处于重大事件暂停期", pair)
                return False

            max_open_trades = int(self.config.get("max_open_trades", 1))
            if max_open_trades <= 0:
                # Freqtrade's lookahead analyzer uses -1 to mean unlimited trades.
                # Keep non-positive limits forbidden outside that read-only utility mode.
                if runmode != "util_no_exchange":
                    logger.error("安全闸门拒绝 %s：最大同时持仓数配置无效", pair)
                    return False
            elif not within_open_trade_limit(Trade.get_open_trade_count(), max_open_trades):
                logger.info("安全闸门拒绝 %s：已达最大同时持仓数", pair)
                return False
            history = [
                TradeResult(t.close_date_utc, float(t.close_profit_abs), str(t.exit_reason or ""))
                for t in Trade.get_trades_proxy(is_open=False)
                if getattr(t, "close_date_utc", None) is not None
            ]
            equity = float(self.wallets.get_total_stake_amount())
            reason = loss_limit_reason(
                now=current_time,
                closed_trades=history,
                account_equity=equity,
                stage_initial_equity=float(self.config.get("dry_run_wallet", 80.0)),
                settings=self._risk_settings(),
            )
            if reason:
                logger.warning("安全闸门拒绝 %s：%s", pair, reason)
                return False

            # Only real-time dry-run entries create user-facing suggestions.
            # Backtests and utility analyses can override the wallet to huge values.
            if runmode == "dry_run":
                suggestion = explain_candidate(pair, candle.to_dict(), float(amount * rate), current_time)
                with self.suggestion_log.open("a", encoding="utf-8") as stream:
                    import json
                    stream.write(json.dumps(suggestion.__dict__, ensure_ascii=False) + "\n")
            return True
        except Exception:
            logger.exception("安全闸门检查失败，拒绝 %s 新开仓", pair)
            return False

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs) -> str | None:
        if current_time - trade.open_date_utc >= timedelta(days=14):
            return "max_holding_14d"
        frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if frame is not None and len(frame):
            candle = frame.iloc[-1]
            if bool(candle.get("trend_invalid", False)):
                return "trend_invalid_close_below_ema20_rsi_lt_45"
        return None

