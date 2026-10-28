"""完全离线的 Binance 公共行情注入与三账户共享回放测试。"""

import json
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hoshi_v11.live_events import LiveMarketJournal, MarketEventError
from hoshi_v11.public_klines import BinancePublicKlineSource, MARKET_DATA_ORIGIN


UTC = timezone.utc
START = datetime(2026, 10, 27, 4, tzinfo=UTC)


def snapshot(at: datetime) -> dict:
    four = at.replace(hour=at.hour - at.hour % 4)
    result = {}
    for pair, price in (("BTC/USDT", 100.0), ("ETH/USDT", 200.0)):
        result[pair] = {}
        for timeframe, close in (("1h", at), ("4h", four)):
            result[pair][timeframe] = {
                "close_time_utc": close.isoformat(), "open": price,
                "high": price + 2, "low": price - 1,
                "close": price + 1, "volume": 12.0,
            }
    return result


class LiveJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = LiveMarketJournal(self.root / "shared.sqlite")
        self.journal.create()

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, at=START, **kwargs):
        return self.journal.ingest(decision_time=at, received_at=at + timedelta(minutes=1),
                                   candles=snapshot(at), **kwargs)

    def test_three_readers_observe_exact_same_event_and_actual_receipt(self):
        published = self.ingest()
        readers = [LiveMarketJournal(self.journal.path) for _ in "ABC"]
        seen = [reader.at(START + timedelta(minutes=2)) for reader in readers]
        self.assertEqual({event.event_id for event in seen}, {published.event_id})
        self.assertEqual({event.received_at_utc for event in seen},
                         {START + timedelta(minutes=1)})
        self.assertEqual({event.one_hour_close_utc for event in seen}, {START})

    def test_4h_future_bar_and_bad_ohlcv_rejected(self):
        future = snapshot(START)
        future["BTC/USDT"]["4h"]["close_time_utc"] = (START + timedelta(hours=4)).isoformat()
        with self.assertRaises(MarketEventError):
            self.journal.ingest(decision_time=START, received_at=START + timedelta(minutes=1),
                                candles=future)
        broken = snapshot(START)
        broken["ETH/USDT"]["1h"]["high"] = 1
        with self.assertRaises(MarketEventError):
            self.journal.ingest(decision_time=START, received_at=START + timedelta(minutes=1),
                                candles=broken)
        self.assertIsNone(self.journal.latest_close())

    def test_duplicate_fault_invalidates_old_snapshot_until_new_event(self):
        self.ingest()
        with self.assertRaisesRegex(MarketEventError, "重复"):
            self.ingest()
        with self.assertRaises(MarketEventError):
            self.journal.at(START + timedelta(minutes=2))
        next_hour = START + timedelta(hours=1)
        self.ingest(next_hour)
        self.assertEqual(self.journal.at(next_hour + timedelta(minutes=2)).one_hour_close_utc,
                         next_hour)

    def test_gap_out_of_order_and_stale_rejected(self):
        self.ingest()
        with self.assertRaisesRegex(MarketEventError, "缺口"):
            self.ingest(START + timedelta(hours=2))
        with self.assertRaisesRegex(MarketEventError, "乱序"):
            self.ingest(START - timedelta(hours=1))
        with self.assertRaises(MarketEventError):
            self.journal.at(START + timedelta(minutes=16))
        self.ingest(START + timedelta(hours=1))
        with self.assertRaisesRegex(MarketEventError, "过期"):
            self.journal.ingest(decision_time=START + timedelta(hours=2),
                                received_at=START + timedelta(hours=2, minutes=16),
                                candles=snapshot(START + timedelta(hours=2)))

    def test_export_and_replay_preserve_event_ids_order_and_fault(self):
        first = self.ingest()
        with self.assertRaises(MarketEventError):
            self.ingest()
        second = self.ingest(START + timedelta(hours=1))
        path = self.root / "audit.jsonl"
        self.assertEqual(self.journal.export_jsonl(path), 3)
        copy = LiveMarketJournal.replay_jsonl(path, self.root / "replayed.sqlite")
        self.assertEqual(copy.at(START + timedelta(hours=1, minutes=2)).event_id,
                         second.event_id)
        self.assertEqual(first.event_id, json.loads(path.read_text(encoding="utf-8").splitlines()[0])
                         ["payload"]["event_id"])
        with self.assertRaises(MarketEventError):
            copy.export_jsonl(path)
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        lines[0]["observed_at_utc"] = START.isoformat()
        tampered = self.root / "tampered.jsonl"
        tampered.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
        with self.assertRaisesRegex(MarketEventError, "接收时刻"):
            LiveMarketJournal.replay_jsonl(tampered, self.root / "tampered.sqlite")

    def test_missing_or_corrupt_journal_fails_closed(self):
        with self.assertRaises(MarketEventError):
            LiveMarketJournal(self.root / "missing.sqlite").at(START)
        self.journal.path.write_bytes(b"not sqlite")
        with self.assertRaises(MarketEventError):
            self.journal.at(START)


class PublicKlineInjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = LiveMarketJournal(Path(self.temp.name) / "shared.sqlite")
        self.journal.create()
        self.urls = []
        self.now = START + timedelta(minutes=1)

        def fake_get(url: str) -> bytes:
            self.urls.append(url)
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)
            opened = int(query["startTime"][0])
            end = int(query["endTime"][0])
            return json.dumps([[opened, "100", "102", "99", "101", "12", end]]).encode()

        self.source = BinancePublicKlineSource(
            self.journal, transport=fake_get, clock=lambda: self.now
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_public_get_only_no_credentials_and_order_endpoint(self):
        event = self.source.run_once(decision_time=START)
        self.assertEqual(event.received_at_utc, self.now)
        self.assertEqual(len(self.urls), 4)
        self.assertTrue(all(url.startswith(MARKET_DATA_ORIGIN + "/api/v3/klines?")
                            for url in self.urls))
        self.assertTrue(all("signature" not in url and "apiKey" not in url
                            and "/order" not in url for url in self.urls))

    def test_disconnect_recovery_backfills_in_order(self):
        self.source.run_once(decision_time=START)
        self.now = START + timedelta(hours=3, minutes=1)
        events = self.source.recover_until(decision_time=START + timedelta(hours=3))
        self.assertEqual([event.one_hour_close_utc for event in events],
                         [START + timedelta(hours=i) for i in (1, 2, 3)])
        self.assertEqual(self.journal.at(self.now).event_id, events[-1].event_id)


if __name__ == "__main__":
    unittest.main()
