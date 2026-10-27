import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from hoshi.indicators import add_1h_indicators, entry_mask, exit_mask, merge_closed_informative
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


ROOT = Path(__file__).resolve().parents[1]
PARAMS = {
    "ema_fast": 20,
    "ema_slow": 50,
    "rsi_period": 14,
    "rsi_max": 65,
    "volume_period": 20,
    "volume_multiplier": 1.1,
    "max_candle_gain": 0.03,
}


class IndicatorTests(unittest.TestCase):
    def test_entry_requires_every_documented_condition(self):
        candle = {
            "trend_ok_4h": True,
            "rsi_cross_up_50": True,
            "rsi14": 55.0,
            "close_cross_up_ema20": True,
            "volume": 120.0,
            "volume_mean_prev20": 100.0,
            "candle_change": 0.02,
        }
        frame = pd.DataFrame([candle])
        self.assertTrue(bool(entry_mask(frame, PARAMS).iloc[0]))
        for column, bad_value in [
            ("trend_ok_4h", False), ("rsi_cross_up_50", False), ("rsi14", 66.0),
            ("close_cross_up_ema20", False), ("volume", 110.0), ("candle_change", 0.031),
        ]:
            altered = frame.copy()
            altered.loc[0, column] = bad_value
            self.assertFalse(bool(entry_mask(altered, PARAMS).iloc[0]), column)

    def test_exit_is_close_below_ema20_and_rsi_below_45(self):
        frame = pd.DataFrame({"trend_invalid": [True, False]})
        self.assertEqual(exit_mask(frame).tolist(), [True, False])

    def test_indicator_prefix_does_not_change_when_future_rows_are_added(self):
        close = pd.Series([100 + i * 0.07 + (i % 4) * 0.2 for i in range(100)])
        base = pd.DataFrame({"open": close * 0.999, "close": close, "volume": [100.0] * len(close)})
        longer = pd.concat([base, pd.DataFrame({"open": [120.0] * 5, "close": [121.0] * 5, "volume": [500.0] * 5})], ignore_index=True)
        first = add_1h_indicators(base, PARAMS)
        second = add_1h_indicators(longer, PARAMS).iloc[: len(base)]
        pd.testing.assert_series_equal(first["ema20"], second["ema20"])
        pd.testing.assert_series_equal(first["rsi14"], second["rsi14"])
        pd.testing.assert_series_equal(first["volume_mean_prev20"], second["volume_mean_prev20"])

    def test_higher_timeframe_row_is_not_visible_until_its_close(self):
        base = pd.DataFrame({"date": pd.to_datetime(["2026-01-01T00:00Z", "2026-01-01T03:00Z", "2026-01-01T04:00Z"]), "close": [1, 2, 3]})
        higher = pd.DataFrame({"date": pd.to_datetime(["2026-01-01T00:00Z"]), "trend_ok": [True]})
        merged = merge_closed_informative(base, higher)
        self.assertTrue(pd.isna(merged.loc[0, "trend_ok_informative"]))
        self.assertTrue(pd.isna(merged.loc[1, "trend_ok_informative"]))
        self.assertTrue(bool(merged.loc[2, "trend_ok_informative"]))


class RiskTests(unittest.TestCase):
    def test_position_is_limited_to_twenty_percent_and_one_percent_risk_budget(self):
        self.assertAlmostEqual(planned_stake(80, 80, RiskSettings()), 16.0)
        self.assertEqual(planned_stake(7, 80, RiskSettings()), 7.0)
        self.assertEqual(planned_stake(0, 80, RiskSettings()), 0.0)

    def test_minimum_amount_and_precision_are_checked(self):
        self.assertIsNotNone(minimum_order_reason(8, 10))
        self.assertIsNotNone(minimum_order_reason(8, None, amount_step=0.01, price=1000))
        self.assertIsNotNone(minimum_order_reason(10.5, 10.5, amount_step=0.01, price=100))
        self.assertIsNone(minimum_order_reason(16, 10, amount_step=0.0001, price=1000))

    def test_maximum_open_trade_limit(self):
        self.assertTrue(within_open_trade_limit(0, 1))
        self.assertFalse(within_open_trade_limit(1, 1))
        self.assertFalse(within_open_trade_limit(0, 0))

    def test_stale_future_or_abnormal_market_data_is_rejected(self):
        now = datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)
        common = dict(current_time=now, reference_price=100.0, current_price=100.1, candle_change=0.01)
        self.assertIsNone(validate_market(candle_time=now - timedelta(minutes=5), **common))
        self.assertIn("过期", validate_market(candle_time=now - timedelta(minutes=76), **common))
        self.assertIn("未来", validate_market(candle_time=now + timedelta(minutes=1), **common))
        self.assertIn("异常", validate_market(candle_time=now - timedelta(minutes=5), **{**common, "candle_change": 0.11}))
        self.assertIn("偏离", validate_market(candle_time=now - timedelta(minutes=5), **{**common, "current_price": 101.0}))

    def test_daily_stoploss_weekly_loss_and_stage_loss_limits(self):
        now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
        stopped = TradeResult(now - timedelta(hours=1), -0.7, "stop_loss")
        self.assertIn("止损", loss_limit_reason(now=now, closed_trades=[stopped], account_equity=79.3))
        weekly_loss = TradeResult(now - timedelta(days=1), -2.5, "exit_signal")
        self.assertIn("本周", loss_limit_reason(now=now, closed_trades=[weekly_loss], account_equity=77.5))
        stage_loss = TradeResult(now - timedelta(days=40), -4.2, "exit_signal")
        self.assertIn(
            "终止本阶段",
            loss_limit_reason(
                now=now,
                closed_trades=[stage_loss],
                account_equity=75.8,
                stage_initial_equity=80.0,
            ),
        )

    def test_event_blackout_file_blocks_only_new_entries(self):
        windows = read_event_blackouts(ROOT / "user_data/hoshi/event_blackouts.csv")
        blocked = event_blackout_reason(datetime(2026, 10, 28, 18, tzinfo=timezone.utc), windows)
        allowed = event_blackout_reason(datetime(2026, 10, 28, 11, 59, tzinfo=timezone.utc), windows)
        self.assertIn("FOMC", blocked)
        self.assertIsNone(allowed)


class SafetyConfigurationTests(unittest.TestCase):
    def test_config_is_spot_dryrun_without_exchange_credentials_or_sol_trading(self):
        config = json.loads((ROOT / "user_data/config.json").read_text(encoding="utf-8"))
        self.assertIs(config["dry_run"], True)
        self.assertEqual(config["trading_mode"], "spot")
        self.assertEqual(config["max_open_trades"], 1)
        self.assertEqual(config["entry_pricing"]["price_side"], "same")
        self.assertEqual(config["exit_pricing"]["price_side"], "same")
        self.assertEqual(config["exchange"]["key"], "")
        self.assertEqual(config["exchange"]["secret"], "")
        self.assertEqual(config["exchange"]["pair_whitelist"], ["BTC/USDT", "ETH/USDT"])
        params = config["strategy_parameters"]
        self.assertEqual(params["max_drawdown_limit"], 0.05)
        self.assertEqual(params["max_drawdown_lookback_candles"], 100000)
        self.assertEqual(params["stage_loss_limit_fraction"], 0.05)
        strategy = (ROOT / "user_data/strategies/HoshiSpotV1.py").read_text(encoding="utf-8")
        self.assertIn("def protections(self)", strategy)
        self.assertIn('"calculation_mode": "equity"', strategy)
        self.assertIn('"util_no_exchange"', strategy)
        self.assertIn('if runmode == "dry_run":', strategy)
        self.assertFalse(any("withdraw" in key.lower() or "transfer" in key.lower() for key in config))

    def test_compose_forces_dryrun_and_binds_webui_to_localhost(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("--dry-run", compose)
        self.assertIn('FREQTRADE__DRY_RUN: "true"', compose)
        self.assertIn('127.0.0.1:8080:8080', compose)
        self.assertIn("freqtradeorg/freqtrade:2026.9", compose)

    def test_strategy_has_no_order_submission_or_withdrawal_calls(self):
        source = (ROOT / "user_data/strategies/HoshiSpotV1.py").read_text(encoding="utf-8").lower()
        for forbidden in ("create_order(", "private_post_", "withdraw(", "transfer("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
