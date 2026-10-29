"""风险账本故障注入：任何数据库异常都禁止新开仓。"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from hoshi_v11.risk import ClosedTrade, EquityInput, RiskEngine
from hoshi_v11.risk.state_store import RiskStateError, StateStore


START = datetime(2026, 10, 26, tzinfo=timezone.utc)


class SQLiteFailureTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Path(self.folder.name) / "A.sqlite"
        self.engine = RiskEngine(account_id="A", db_path=self.db)
        self.engine.bootstrap(stage_id="A-test", initial_equity=80, started_at=START)

    def tearDown(self):
        self.folder.cleanup()

    def evaluate(self, at=START, *, cash=80, trades=()):
        return self.engine.evaluate(
            now=at, equity_input=EquityInput(cash), closed_trades=tuple(trades),
            market_event_id=f"event:{at.isoformat()}", last_closed_candle_at=at,
        )

    def marker(self):
        return self.db.with_suffix(".sqlite.emergency.json")

    def test_connection_interruption_latches_across_restart(self):
        with patch.object(self.engine.store, "connect",
                          side_effect=sqlite3.OperationalError("connection interrupted")):
            denied = self.evaluate()
        self.assertEqual(denied.state, "EMERGENCY_HALT")
        self.assertFalse(denied.allow_new_entry)
        self.assertTrue(self.marker().is_file())
        restarted = RiskEngine(account_id="A", db_path=self.db)
        self.assertEqual(restarted.evaluate(
            now=START + timedelta(hours=1), equity_input=EquityInput(80),
            closed_trades=(), market_event_id="next", last_closed_candle_at=START + timedelta(hours=1),
        ).state, "EMERGENCY_HALT")

    def test_writer_lock_competition_fails_closed(self):
        holder = sqlite3.connect(self.db)
        try:
            holder.execute("BEGIN IMMEDIATE")
            with patch("hoshi_v11.risk.state_store.BUSY_TIMEOUT_MS", 100):
                denied = self.evaluate()
            self.assertEqual(denied.state, "EMERGENCY_HALT")
            self.assertFalse(denied.allow_new_entry)
        finally:
            holder.rollback()
            holder.close()
        self.assertTrue(self.marker().is_file())
        self.assertEqual(RiskEngine(account_id="A", db_path=self.db).evaluate(
            now=START + timedelta(hours=1), equity_input=EquityInput(80),
            closed_trades=(), market_event_id="next", last_closed_candle_at=START + timedelta(hours=1),
        ).state, "EMERGENCY_HALT")

    def test_failed_write_rolls_back_sample_and_persists_manual_halt(self):
        with patch.object(self.engine, "_decision",
                          side_effect=sqlite3.OperationalError("injected write failure")):
            denied = self.evaluate()
        self.assertEqual(denied.state, "EMERGENCY_HALT")
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM equity_marks").fetchone()[0], 0)
            halt = json.loads(connection.execute(
                "SELECT value FROM meta WHERE key='manual_halt'"
            ).fetchone()[0])
        self.assertTrue(halt.startswith("EMERGENCY:"))
        self.assertTrue(self.marker().is_file())

    def test_crash_before_commit_automatically_rolls_back_and_restarts(self):
        script = """import os, sqlite3, sys
c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE')
c.execute('INSERT INTO equity_marks(observed_at,market_event_id,source,equity,stage_peak,stage_drawdown) VALUES (?,?,?,?,?,?)', ('2026-10-26T00:00:00+00:00','uncommitted','candle_close',80,80,0))
os._exit(17)
"""
        child = subprocess.run([sys.executable, "-c", script, str(self.db)],
                               capture_output=True, timeout=5, check=False)
        self.assertEqual(child.returncode, 17, child.stderr)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM equity_marks").fetchone()[0], 0)
        restarted = RiskEngine(account_id="A", db_path=self.db)
        self.assertEqual(restarted.evaluate(
            now=START, equity_input=EquityInput(80), closed_trades=(),
            market_event_id="recovered", last_closed_candle_at=START,
        ).state, "ACTIVE")

    def test_corruption_and_trade_mismatch_stay_halted(self):
        closed_at = START + timedelta(hours=1)
        trade = ClosedTrade("t1", closed_at, -0.2, "trend_invalid")
        self.assertEqual(self.evaluate(closed_at, cash=79.8, trades=(trade,)).state, "ACTIVE")
        denied = self.evaluate(closed_at + timedelta(hours=1), cash=79.8, trades=())
        self.assertEqual(denied.state, "EMERGENCY_HALT")
        with closing(sqlite3.connect(self.db)) as connection:
            halt = json.loads(connection.execute(
                "SELECT value FROM meta WHERE key='manual_halt'"
            ).fetchone()[0])
        self.assertTrue(halt.startswith("EMERGENCY:"))
        other = Path(self.folder.name) / "corrupt.sqlite"
        other.write_bytes(b"not SQLite")
        corrupted = RiskEngine(account_id="A", db_path=other).evaluate(
            now=START, equity_input=EquityInput(80), closed_trades=(),
            market_event_id="event", last_closed_candle_at=START,
        )
        self.assertEqual(corrupted.state, "EMERGENCY_HALT")
        self.assertTrue(other.with_suffix(".sqlite.emergency.json").is_file())

    def test_nested_write_transaction_is_rejected(self):
        connection = self.engine.store.connect(account_id="A", policy_hash=self.engine.policy.hash)
        try:
            self.engine.store.begin_write(connection)
            with self.assertRaises(RiskStateError):
                self.engine.store.begin_write(connection)
            connection.rollback()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
