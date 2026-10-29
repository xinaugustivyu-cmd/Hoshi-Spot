import sys
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "user_data/strategies"))

from HoshiSpotV11A import HoshiSpotV11A
from hoshi_v11.freqtrade_strategy import MarketDataFault
from hoshi_v11.event_model import MarketEvent
from hoshi_v11.live_events import MarketEventError
from hoshi_v11.risk import RiskPolicy
from hoshi_v11.risk.models import RiskDecision


UTC = timezone.utc
NOW = datetime(2026, 1, 1, 1, tzinfo=UTC)


class CandidateAdapterSafetyTests(unittest.TestCase):
    def make_strategy(self, *, allow_entry=True):
        strategy = HoshiSpotV11A.__new__(HoshiSpotV11A)
        strategy.config = {"runmode": "backtest", "dry_run": True, "trading_mode": "spot"}
        frame = pd.DataFrame([{
            "date": pd.Timestamp(datetime(2026, 1, 1, 0, tzinfo=UTC)),
            "close": 100.0, "candle_change": 0.01, "trend_invalid": True,
        }])
        strategy.dp = SimpleNamespace(get_analyzed_dataframe=lambda pair, timeframe: (frame, NOW))
        strategy.wallets = SimpleNamespace(get_free=lambda currency: 80.0)
        strategy.event_file = ROOT / "user_data/hoshi/event_blackouts.csv"
        strategy.risk_engine = None
        policy = RiskPolicy()
        decision = RiskDecision(
            account_id="A", state="ACTIVE" if allow_entry else "HALTED_MANUAL_REVIEW",
            state_id="state-1", triggered_at=NOW, rule="test", details={"equity": 80},
            pause_until=None, approval_id=None, allow_new_entry=allow_entry,
            allow_exit=True, auto_resume=False,
            policy_version=policy.version, policy_hash=policy.hash,
        )
        strategy._assess = lambda current_time: decision
        return strategy

    def test_entry_gate_rejects_notional_above_16(self):
        strategy = self.make_strategy()
        with patch("hoshi_v11.freqtrade_strategy.Trade.get_open_trade_count", return_value=0):
            rejected = strategy.confirm_trade_entry(
                "BTC/USDT", "limit", 0.20, 100.0, "gtc", NOW,
                "v1", "long",
            )
            accepted = strategy.confirm_trade_entry(
                "BTC/USDT", "limit", 0.10, 100.0, "gtc", NOW,
                "v1", "long",
            )
        self.assertFalse(rejected)
        self.assertTrue(accepted)

    def test_manual_halt_does_not_disable_existing_position_exit(self):
        strategy = self.make_strategy(allow_entry=False)
        trade = SimpleNamespace(open_date_utc=NOW - timedelta(hours=2))
        result = strategy.custom_exit("BTC/USDT", trade, NOW, 100.0, -0.01)
        self.assertEqual(result, "trend_invalid_close_below_ema20_rsi_lt_45")

    def test_blocked_signal_is_audited_before_stake_returns_zero(self):
        strategy = self.make_strategy(allow_entry=False)
        strategy.risk_engine = Mock()
        strategy._last_market_event_id = "shared-event-id"
        stake = strategy.custom_stake_amount(
            "BTC/USDT", NOW, 100.0, 16.0, 10.0, 80.0, 1.0,
            "v1", "long",
        )
        self.assertEqual(stake, 0.0)
        strategy.risk_engine.record_blocked_candidate.assert_called_once()
        self.assertEqual(
            strategy.risk_engine.record_blocked_candidate.call_args.kwargs["market_event_id"],
            "shared-event-id",
        )

    def test_non_dry_run_config_cannot_start_strategy(self):
        strategy = self.make_strategy()
        strategy.config["dry_run"] = False
        strategy.config["exchange"] = {"name": "binance", "key": "", "secret": ""}
        strategy.config["max_open_trades"] = 1
        strategy.config["stake_currency"] = "USDT"
        strategy.config["dry_run_wallet"] = 80
        with self.assertRaises(RuntimeError):
            strategy.bot_start()

    def test_dry_run_start_requires_order_guard_and_fresh_shared_feed(self):
        strategy = HoshiSpotV11A.__new__(HoshiSpotV11A)
        strategy.config = json.loads((ROOT / "user_data/v11_configs/A.json").read_text(encoding="utf-8"))
        strategy.config["runmode"] = "dry_run"
        with patch("hoshi_v11.freqtrade_strategy.is_installed", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "订单隔离未安装"):
                strategy.bot_start()
        with (patch("hoshi_v11.freqtrade_strategy.is_installed", return_value=True),
              patch("hoshi_v11.freqtrade_strategy.Path.is_file", return_value=True),
              patch("hoshi_v11.freqtrade_strategy.LiveMarketJournal.at",
                    side_effect=MarketEventError("共享行情缺失"))):
            with self.assertRaisesRegex(MarketEventError, "共享行情缺失"):
                strategy.bot_start()

    def test_missing_candle_routes_to_persistent_data_pause(self):
        strategy = self.make_strategy()
        strategy.risk_engine = Mock()
        strategy._risk_bootstrap_pending = False
        del strategy._assess
        strategy._risk_inputs = Mock(side_effect=MarketDataFault("missing candle"))
        decision = strategy._assess(NOW)
        strategy.risk_engine.record_data_fault.assert_called_once_with(
            now=NOW, reason="missing candle"
        )
        self.assertIs(decision, strategy.last_risk_decision)

    def test_wallet_reconciliation_fault_routes_to_emergency(self):
        strategy = self.make_strategy()
        strategy.risk_engine = Mock()
        strategy._risk_bootstrap_pending = False
        del strategy._assess
        strategy._risk_inputs = Mock(side_effect=ValueError("wallet mismatch"))
        strategy._assess(NOW)
        strategy.risk_engine.record_emergency.assert_called_once()
        self.assertIn("wallet mismatch",
                      strategy.risk_engine.record_emergency.call_args.kwargs["reason"])

    def test_live_freqtrade_frames_must_match_shared_snapshot_for_both_pairs(self):
        strategy = self.make_strategy()
        four_start = datetime(2025, 12, 31, 20, tzinfo=UTC)
        frames = {}
        candles = {}
        for pair, price in (("BTC/USDT", 100.0), ("ETH/USDT", 200.0)):
            frames[pair] = pd.DataFrame([{
                "date": pd.Timestamp(NOW - timedelta(hours=1)),
                "date_4h": pd.Timestamp(four_start),
                "open": price, "high": price + 2, "low": price - 1,
                "close": price + 1, "volume": 12.0,
                "open_4h": price, "high_4h": price + 2, "low_4h": price - 1,
                "close_4h": price + 1, "volume_4h": 12.0,
            }])
            candles[pair] = {
                "1h": {"close_time_utc": NOW.isoformat(), "open": price,
                       "high": price + 2, "low": price - 1, "close": price + 1,
                       "volume": 12.0},
                "4h": {"close_time_utc": (four_start + timedelta(hours=4)).isoformat(),
                       "open": price, "high": price + 2, "low": price - 1,
                       "close": price + 1, "volume": 12.0},
            }
        strategy.dp = SimpleNamespace(get_analyzed_dataframe=lambda pair, timeframe: (frames[pair], NOW))
        event = MarketEvent("shared-id", NOW, NOW, candles, NOW + timedelta(minutes=1))
        strategy._verify_shared_snapshot(event)
        frames["ETH/USDT"].loc[0, "close_4h"] = 999.0
        with self.assertRaises(MarketDataFault):
            strategy._verify_shared_snapshot(event)


if __name__ == "__main__":
    unittest.main()
