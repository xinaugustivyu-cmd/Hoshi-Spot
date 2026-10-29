"""No-trade fuse and isolated watchdog tests; no trading services are launched."""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hoshi_v11.acceptance_watchdog import AcceptanceWatchdog, Manifest, LABEL


class NoTradeGuardTests(unittest.TestCase):
    def test_all_freqtrade_execution_boundaries_reject_before_original(self):
        code = r'''
from hoshi_v11.order_guard import install as install_network
from hoshi_v11.no_trade_guard import install, is_installed, NoTradeBlocked, BOT_METHODS, EXCHANGE_METHODS
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.exchange.exchange import Exchange
install_network()
install()
install()
assert is_installed()
fake = object()
for cls, methods in ((FreqtradeBot, BOT_METHODS), (Exchange, EXCHANGE_METHODS)):
    for name in methods:
        try:
            getattr(cls, name)(fake)
        except NoTradeBlocked:
            pass
        else:
            raise AssertionError(name + ' reached original execution')
print('all execution and simulated fill paths blocked')
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("all execution and simulated fill paths blocked", result.stdout)


class FakeDocker:
    def __init__(self, run_id, ids, fail_after=None):
        self.run_id = run_id
        self.ids = ids
        self.stopped = []
        self.calls = 0
        self.fail_after = fail_after
        self.running = {item: True for item in ids}

    def inspect(self, cid):
        self.calls += 1
        if self.fail_after and self.calls >= self.fail_after:
            raise RuntimeError("injected Docker failure")
        return {"Id": cid, "Config": {"Labels": {LABEL: self.run_id}},
                "State": {"Running": self.running[cid]}}

    def stop(self, cid):
        self.stopped.append(cid)
        self.running[cid] = False


class AcceptanceWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
        self.ids = ("a" * 64, "b" * 64)
        self.run_id = "v11-acceptance-test-001"
        self.manifest = Manifest(self.run_id, self.start, self.ids)

    def test_timeout_stops_only_owned_containers_and_keeps_data(self):
        docker = FakeDocker(self.run_id, self.ids)
        clock = [self.start]
        watchdog = AcceptanceWatchdog(
            self.manifest, docker, now=lambda: clock[0],
            sleep=lambda _: clock.__setitem__(0, self.start + timedelta(minutes=30)),
        )
        self.assertEqual(watchdog.run(), "timeout")
        self.assertEqual(set(docker.stopped), set(self.ids))
        self.assertFalse(hasattr(docker, "remove"))

    def test_component_exit_stops_remaining_owned_container(self):
        docker = FakeDocker(self.run_id, self.ids)
        docker.running[self.ids[1]] = False
        watchdog = AcceptanceWatchdog(self.manifest, docker, now=lambda: self.start,
                                      sleep=lambda _: None)
        self.assertEqual(watchdog.run(), "component_exit")
        self.assertEqual(set(docker.stopped), set(self.ids))

    def test_docker_failure_stops_preverified_containers(self):
        docker = FakeDocker(self.run_id, self.ids, fail_after=3)
        watchdog = AcceptanceWatchdog(self.manifest, docker, now=lambda: self.start,
                                      sleep=lambda _: None)
        with self.assertRaisesRegex(RuntimeError, "injected Docker failure"):
            watchdog.run()
        self.assertEqual(set(docker.stopped), set(self.ids))

    def test_interrupted_watchdog_stops_owned_containers(self):
        docker = FakeDocker(self.run_id, self.ids)
        def interrupted(_seconds):
            raise KeyboardInterrupt("injected operator interruption")
        watchdog = AcceptanceWatchdog(self.manifest, docker, now=lambda: self.start,
                                      sleep=interrupted)
        with self.assertRaises(KeyboardInterrupt):
            watchdog.run()
        self.assertEqual(set(docker.stopped), set(self.ids))

    def test_foreign_container_rejected_without_stop(self):
        docker = FakeDocker("v11-acceptance-foreign", self.ids)
        watchdog = AcceptanceWatchdog(self.manifest, docker, now=lambda: self.start)
        with self.assertRaisesRegex(RuntimeError, "归属标签"):
            watchdog.run()
        self.assertEqual(docker.stopped, [])

    def test_manifest_limits_and_does_not_modify_database_or_log(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "risk.sqlite"
            log = root / "acceptance.log"
            database.write_bytes(b"risk-record")
            log.write_text("original log", encoding="utf-8")
            path = root / "manifest.json"
            path.write_text(json.dumps({"run_id": self.run_id,
                                        "started_at": self.start.isoformat(),
                                        "container_ids": self.ids}), encoding="utf-8")
            self.assertEqual(Manifest.read(path), self.manifest)
            docker = FakeDocker(self.run_id, self.ids)
            watchdog = AcceptanceWatchdog(self.manifest, docker,
                                          now=lambda: self.start + timedelta(minutes=31))
            self.assertEqual(watchdog.run(), "timeout")
            self.assertEqual(database.read_bytes(), b"risk-record")
            self.assertEqual(log.read_text(encoding="utf-8"), "original log")
            path.write_text(json.dumps({"run_id": "wrong", "started_at": self.start.isoformat(),
                                        "container_ids": self.ids}), encoding="utf-8")
            with self.assertRaises(ValueError):
                Manifest.read(path)


if __name__ == "__main__":
    unittest.main()
