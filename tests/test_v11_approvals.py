import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hoshi_v11.approvals import ApprovalStore, ResearchAdvice


UTC = timezone.utc
NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


class ApprovalOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "reviews.sqlite"
        self.store = ApprovalStore(self.db)
        self.store.initialize()
        self.advice = ResearchAdvice(
            advice_id="advice-1", account_id="A", strategy_version="HoshiSpotV11A",
            signal_id="signal-1", market_event_id="event-1",
            signal_candle_close_utc=NOW - timedelta(minutes=1),
            generated_at_utc=NOW, expires_at_utc=NOW + timedelta(minutes=15),
            pair="BTC/USDT", reference_price=100.0, planned_stake_usdt=16.0,
            planned_take_profit=106.0, planned_stop_loss=96.0,
            reason="研究建议", risk_state_id="state-1", risk_policy_hash="hash-1",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_approved_is_only_database_record_and_cannot_execute_order(self):
        self.store.propose(self.advice)
        result = self.store.review(
            approval_id="human-1", advice_id="advice-1", reviewer="researcher",
            reviewed_at=NOW + timedelta(minutes=1), decision="APPROVED", reason="read only",
        )
        self.assertEqual(result, "APPROVED")
        self.assertEqual(self.store.status("advice-1", NOW + timedelta(minutes=2)), "APPROVED")
        self.assertFalse(hasattr(self.store, "create_order"))
        self.assertFalse(hasattr(self.store, "execute_order"))
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 1)

    def test_pending_expires_and_cannot_be_approved_late(self):
        self.store.propose(self.advice)
        self.assertEqual(self.store.status("advice-1", NOW + timedelta(minutes=15)), "EXPIRED")
        with self.assertRaises(ValueError):
            self.store.review(approval_id="late", advice_id="advice-1", reviewer="r",
                              reviewed_at=NOW + timedelta(minutes=16),
                              decision="APPROVED", reason="late")

    def test_missing_database_is_not_recreated(self):
        self.db.unlink()
        with self.assertRaises(RuntimeError):
            self.store.propose(self.advice)
        self.assertFalse(self.db.exists())

    def test_recommendation_cannot_exceed_16_usdt(self):
        from dataclasses import replace
        with self.assertRaises(ValueError):
            self.store.propose(replace(self.advice, planned_stake_usdt=16.01))


if __name__ == "__main__":
    unittest.main()
