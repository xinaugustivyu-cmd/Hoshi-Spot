"""V1.1 独立风控离线门禁；不连接交易所。"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hoshi_v11.risk import ClosedTrade, EquityInput, PositionMark, RiskEngine, RiskPolicy
from hoshi_v11.risk.equity import planned_stake, simulated_equity


UTC = timezone.utc
MONDAY = datetime(2026, 10, 26, 0, tzinfo=UTC)


class RiskV11Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "account_a.sqlite"
        self.policy = RiskPolicy()
        self.engine = RiskEngine(account_id="A", db_path=self.db, policy=self.policy)
        self.engine.bootstrap(stage_id="A-1", initial_equity=80, started_at=MONDAY)

    def tearDown(self):
        self.temp.cleanup()

    def check(self, at=MONDAY, *, cash=80.0, trades=(), candle=None, **kwargs):
        return self.engine.evaluate(
            now=at, equity_input=EquityInput(cash), closed_trades=tuple(trades),
            market_event_id=f"BTC-1h:{at.isoformat()}",
            last_closed_candle_at=at if candle is None else candle,
            **kwargs,
        )

    def test_stake_enforces_16_twenty_percent_one_percent_and_one_position(self):
        self.assertEqual(planned_stake(available_cash=80, equity=80, open_trades=0, policy=self.policy), 16)
        self.assertEqual(planned_stake(available_cash=80, equity=40, open_trades=0, policy=self.policy), 8)
        self.assertEqual(planned_stake(available_cash=80, equity=80, open_trades=1, policy=self.policy), 0)
        self.assertEqual(planned_stake(available_cash=80, equity=80, open_trades=0, policy=self.policy, leverage=2), 0)
        self.assertEqual(planned_stake(available_cash=80, equity=80, open_trades=0, policy=self.policy, trading_mode="futures"), 0)
        with self.assertRaises(ValueError):
            RiskPolicy(max_stake_usdt=17)

    def test_equity_marks_open_position_and_exit_fee_once(self):
        value = EquityInput(64, (PositionMark("BTC/USDT", 0.16, 100, 0.001, 16),))
        self.assertAlmostEqual(simulated_equity(value), 79.984)
        result = self.engine.evaluate(
            now=MONDAY, equity_input=value, closed_trades=(), market_event_id="c0",
            last_closed_candle_at=MONDAY,
        )
        self.assertEqual(result.state, "ACTIVE")
        self.assertAlmostEqual(result.details["equity"], 79.984)

    def test_weekly_pause_and_monday_utc_boundary(self):
        sunday = MONDAY + timedelta(days=6, hours=23)
        first = self.check(sunday)
        self.assertEqual(first.state, "ACTIVE")
        trade = ClosedTrade("w1", sunday, -2.4, "trend_invalid")
        paused = self.check(sunday, cash=77.6, trades=(trade,))
        self.assertEqual(paused.state, "WEEKLY_PAUSE")
        self.assertEqual(paused.pause_until, MONDAY + timedelta(days=7))
        next_week = self.check(MONDAY + timedelta(days=7), cash=77.6, trades=(trade,))
        self.assertEqual(next_week.state, "ACTIVE")
        with sqlite3.connect(self.db) as connection:
            openings = connection.execute("SELECT opening_equity FROM week_snapshots ORDER BY week_start").fetchall()
        self.assertEqual(openings, [(80.0,), (77.6,)])

    def test_stoploss_cooldown_uses_later_of_24h_and_utc_day(self):
        close = MONDAY + timedelta(hours=23)
        stop = ClosedTrade("s1", close, -0.7, "stop_loss")
        result = self.check(close, cash=79.3, trades=(stop,))
        self.assertEqual(result.state, "COOLDOWN")
        self.assertEqual(result.pause_until, close + timedelta(hours=24))
        after = self.check(close + timedelta(hours=24), cash=79.3, trades=(stop,))
        self.assertEqual(after.state, "ACTIVE")

    def test_rolling_14d_boundary_excludes_exact_start(self):
        end = MONDAY + timedelta(days=14)
        old = ClosedTrade("old", MONDAY, -4.0, "trend_invalid")
        # A sample at day 14 needs weekly snapshot continuity. Test the policy
        # expression with a complete sequence of hourly equity samples.
        for hour in range(14 * 24 + 1):
            at = MONDAY + timedelta(hours=hour)
            result = self.check(at, cash=76 if hour else 80, trades=(old,) if hour else ())
        self.assertEqual(at, end)
        self.assertEqual(result.details["rolling_14d_net"], 0)

    def test_rolling_loss_manual_halt_persists_after_restart_and_window_expiry(self):
        at = MONDAY + timedelta(hours=2)
        trade = ClosedTrade("r1", at, -4.0, "trend_invalid")
        halted = self.check(at, cash=76, trades=(trade,))
        self.assertEqual(halted.state, "HALTED_MANUAL_REVIEW")
        restarted = RiskEngine(account_id="A", db_path=self.db, policy=self.policy)
        later = at + timedelta(days=15)
        # In this test keep weekly snapshots complete over the restart gap by
        # inserting only the relevant weekly boundary samples.
        for hour in range(1, 15 * 24 + 1):
            moment = at + timedelta(hours=hour)
            current = restarted.evaluate(
                now=moment, equity_input=EquityInput(76), closed_trades=(trade,),
                market_event_id=f"c{hour}", last_closed_candle_at=moment,
            )
        self.assertEqual(moment, later)
        self.assertEqual(current.details["rolling_14d_net"], 0)
        self.assertEqual(current.state, "HALTED_MANUAL_REVIEW")
        self.assertFalse(current.allow_new_entry)
        self.assertTrue(current.allow_exit)

    def test_manual_halt_outweighs_weekly_and_records_blocked_signal(self):
        trade = ClosedTrade("both", MONDAY, -4, "trend_invalid")
        decision = self.check(MONDAY, cash=76, trades=(trade,), market_valid=False)
        self.assertEqual(decision.state, "HALTED_MANUAL_REVIEW")
        self.engine.record_blocked_candidate(
            now=MONDAY, signal_id="signal-1", market_event_id="event-1",
            decision=decision,
        )
        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                "SELECT details_json FROM risk_events WHERE rule='blocked_candidate'"
            ).fetchone()
        self.assertIn("signal-1", row[0])

    def test_manual_resume_rejected_while_14d_threshold_remains(self):
        at = MONDAY + timedelta(hours=1)
        trade = ClosedTrade("r1", at, -4, "trend_invalid")
        self.check(at, cash=76, trades=(trade,))
        outcome = self.engine.resume_after_review(
            approval_id="approval-1", reviewer="human", reason="reviewed",
            new_stage_id="A-2", now=at, equity_input=EquityInput(76),
            closed_trades=(trade,),
        )
        self.assertEqual(outcome.state, "HALTED_MANUAL_REVIEW")
        self.assertFalse(outcome.allow_new_entry)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 0)

    def test_drawdown_triggers_manual_stop_and_preserves_exit(self):
        win = ClosedTrade("win", MONDAY, 4, "roi")
        loss = ClosedTrade("loss", MONDAY + timedelta(hours=1), -4.2, "trend_invalid")
        self.check(MONDAY, cash=84, trades=(win,))
        result = self.check(MONDAY + timedelta(hours=1), cash=79.8, trades=(win, loss))
        self.assertEqual(result.state, "HALTED_MANUAL_REVIEW")
        self.assertIn("max_drawdown", result.rule)
        self.assertFalse(result.allow_new_entry)
        self.assertTrue(result.allow_exit)
        self.assertAlmostEqual(result.details["drawdown"], 0.05)

    def test_data_pause_needs_two_new_closed_candles(self):
        bad = self.check(MONDAY, market_valid=False)
        self.assertEqual(bad.state, "DATA_PAUSE")
        same = self.check(MONDAY)
        self.assertEqual(same.state, "DATA_PAUSE")
        one = self.check(MONDAY + timedelta(hours=1))
        self.assertEqual(one.state, "DATA_PAUSE")
        two = self.check(MONDAY + timedelta(hours=2))
        self.assertEqual(two.state, "ACTIVE")

    def test_missing_candle_fault_is_durable_and_needs_two_later_candles(self):
        bad = self.engine.record_data_fault(now=MONDAY, reason="1h candle missing")
        self.assertEqual(bad.state, "DATA_PAUSE")
        self.assertFalse(bad.allow_new_entry)
        restarted = RiskEngine(account_id="A", db_path=self.db, policy=self.policy)
        first = restarted.evaluate(
            now=MONDAY + timedelta(hours=1), equity_input=EquityInput(80),
            closed_trades=(), market_event_id="healthy-1",
            last_closed_candle_at=MONDAY + timedelta(hours=1),
        )
        second = restarted.evaluate(
            now=MONDAY + timedelta(hours=2), equity_input=EquityInput(80),
            closed_trades=(), market_event_id="healthy-2",
            last_closed_candle_at=MONDAY + timedelta(hours=2),
        )
        self.assertEqual(first.state, "DATA_PAUSE")
        self.assertEqual(second.state, "ACTIVE")
        with sqlite3.connect(self.db) as connection:
            self.assertGreaterEqual(connection.execute(
                "SELECT COUNT(*) FROM risk_events WHERE rule='invalid_market_data'"
            ).fetchone()[0], 1)

    def test_missing_database_fails_closed_without_recreation(self):
        self.db.unlink()
        outcome = self.check()
        self.assertEqual(outcome.state, "EMERGENCY_HALT")
        self.assertFalse(self.db.exists())
        self.assertTrue(self.db.with_suffix(".sqlite.emergency.json").exists())

    def test_corrupted_database_fails_closed(self):
        self.db.write_bytes(b"not a database")
        outcome = self.check()
        self.assertEqual(outcome.state, "EMERGENCY_HALT")

    def test_trade_history_mismatch_fails_closed(self):
        at = MONDAY + timedelta(hours=1)
        trade = ClosedTrade("t1", at, -0.3, "trend_invalid")
        self.check(at, cash=79.7, trades=(trade,))
        missing = self.check(at + timedelta(hours=1), cash=79.7, trades=())
        self.assertEqual(missing.state, "EMERGENCY_HALT")
        altered = self.check(at + timedelta(hours=1), cash=79.7,
                             trades=(ClosedTrade("t1", at, 1.0, "trend_invalid"),))
        self.assertEqual(altered.state, "EMERGENCY_HALT")
        corrected = self.check(at + timedelta(hours=2), cash=79.7, trades=(trade,))
        self.assertEqual(corrected.state, "EMERGENCY_HALT")

    def test_duplicate_candle_event_pauses_until_two_new_closes(self):
        self.check(MONDAY)
        duplicate = self.engine.evaluate(
            now=MONDAY, equity_input=EquityInput(80), closed_trades=(),
            market_event_id="different-payload-same-hour",
            last_closed_candle_at=MONDAY,
        )
        self.assertEqual(duplicate.state, "DATA_PAUSE")
        self.assertEqual(self.check(MONDAY + timedelta(hours=1)).state, "DATA_PAUSE")
        self.assertEqual(self.check(MONDAY + timedelta(hours=2)).state, "ACTIVE")

    def test_manual_resume_creates_new_stage_without_deleting_history(self):
        first = MONDAY + timedelta(hours=1)
        win = ClosedTrade("win", MONDAY, 4.0, "roi")
        loss = ClosedTrade("loss", first, -4.2, "trend_invalid")
        history = (win, loss)
        self.check(MONDAY, cash=84, trades=(win,))
        stopped = self.check(first, cash=79.8, trades=history)
        self.assertEqual(stopped.state, "HALTED_MANUAL_REVIEW")
        resumed = self.engine.resume_after_review(
            approval_id="approval-2", reviewer="research-owner",
            reason="reviewed drawdown; simulation only", new_stage_id="A-2",
            now=first, equity_input=EquityInput(79.8), closed_trades=history,
        )
        self.assertEqual(resumed.state, "ACTIVE")
        after = self.check(first + timedelta(hours=1), cash=79.8, trades=history)
        self.assertEqual(after.state, "ACTIVE")
        self.assertEqual(after.details["stage_id"], "A-2")
        self.assertAlmostEqual(after.details["rolling_14d_net"], -0.2)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 1)

    def test_three_accounts_have_distinct_policy_hash_and_ledgers(self):
        with tempfile.TemporaryDirectory() as folder:
            others = []
            for name in ("B", "C"):
                engine = RiskEngine(account_id=name, db_path=Path(folder) / f"{name}.sqlite",
                                    policy=self.policy)
                engine.bootstrap(stage_id=f"{name}-1", initial_equity=80, started_at=MONDAY)
                others.append(engine)
            self.assertEqual(len({str(self.db), *[str(e.store.path) for e in others]}), 3)
            self.assertEqual({self.policy.hash, *[e.policy.hash for e in others]}, {self.policy.hash})
            trade = ClosedTrade("a-loss", MONDAY, -4, "trend_invalid")
            self.assertEqual(self.check(cash=76, trades=(trade,)).state, "HALTED_MANUAL_REVIEW")
            for engine in others:
                decision = engine.evaluate(
                    now=MONDAY, equity_input=EquityInput(80), closed_trades=(),
                    market_event_id="c", last_closed_candle_at=MONDAY,
                )
                self.assertEqual(decision.state, "ACTIVE")

    def test_real_trading_and_credentials_fail_closed(self):
        for override in ({"dry_run": False}, {"trading_mode": "futures"},
                         {"leverage": 2.0}, {"exchange_key": "secret"},
                         {"exchange_name": "other"}):
            with self.subTest(override=override):
                with tempfile.TemporaryDirectory() as folder:
                    engine = RiskEngine(account_id="A", db_path=Path(folder) / "risk.sqlite")
                    engine.bootstrap(stage_id="A-1", initial_equity=80, started_at=MONDAY)
                    result = engine.evaluate(
                        now=MONDAY, equity_input=EquityInput(80), closed_trades=(),
                        market_event_id="c", last_closed_candle_at=MONDAY, **override,
                    )
                    self.assertEqual(result.state, "EMERGENCY_HALT")
                    self.assertFalse(result.allow_new_entry)

    def test_account_reconciliation_failure_is_manual_emergency(self):
        result = self.check(account_reconciled=False)
        self.assertEqual(result.state, "EMERGENCY_HALT")
        later = self.check(MONDAY + timedelta(hours=1))
        self.assertEqual(later.state, "EMERGENCY_HALT")

    def test_cash_without_realized_trade_cannot_create_fake_high_water(self):
        result = self.check(MONDAY, cash=84)
        self.assertEqual(result.state, "EMERGENCY_HALT")
        self.assertFalse(result.allow_new_entry)

    def test_policy_hash_rejects_different_risk_configuration(self):
        other = RiskEngine(account_id="A", db_path=self.db, policy=RiskPolicy(weekly_loss_fraction=0.02))
        decision = other.evaluate(now=MONDAY, equity_input=EquityInput(80),
                                  closed_trades=(), market_event_id="c",
                                  last_closed_candle_at=MONDAY)
        self.assertEqual(decision.state, "EMERGENCY_HALT")


if __name__ == "__main__":
    unittest.main()
