import sys
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


if __name__ == "__main__":
    unittest.main()
