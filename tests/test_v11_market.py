import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from hoshi_v11.market_events import HistoricalEventFeed


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)


class SharedMarketEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        for pair in ("BTC_USDT", "ETH_USDT"):
            base = 100 if pair.startswith("BTC") else 200
            one = pd.DataFrame({
                "date": [START + timedelta(hours=i) for i in range(8)],
                "open": [base + i for i in range(8)],
                "high": [base + i + 2 for i in range(8)],
                "low": [base + i - 1 for i in range(8)],
                "close": [base + i + 1 for i in range(8)],
                "volume": [10 + i for i in range(8)],
            })
            four = one.iloc[[0, 4]].copy().reset_index(drop=True)
            one.to_feather(self.folder / f"{pair}-1h.feather")
            four.to_feather(self.folder / f"{pair}-4h.feather")

    def tearDown(self):
        self.temp.cleanup()

    def test_four_hour_candle_is_not_visible_before_close(self):
        feed = HistoricalEventFeed(self.folder)
        with self.assertRaisesRegex(ValueError, "缺失或过期"):
            feed.at(START + timedelta(hours=3))
        at_four = feed.at(START + timedelta(hours=4))
        self.assertEqual(at_four.candles["BTC/USDT"]["4h"]["close_time_utc"],
                         (START + timedelta(hours=4)).isoformat())
        at_seven = feed.at(START + timedelta(hours=7))
        self.assertEqual(at_seven.candles["BTC/USDT"]["4h"]["close_time_utc"],
                         (START + timedelta(hours=4)).isoformat())
        at_eight = feed.at(START + timedelta(hours=8))
        self.assertEqual(at_eight.candles["BTC/USDT"]["4h"]["close_time_utc"],
                         (START + timedelta(hours=8)).isoformat())

    def test_identical_input_produces_same_event_id(self):
        first = HistoricalEventFeed(self.folder).at(START + timedelta(hours=5))
        second = HistoricalEventFeed(self.folder).at(START + timedelta(hours=5))
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(first.one_hour_close_utc, START + timedelta(hours=5))
        self.assertIsNone(first.received_at_utc)

    def test_gap_rejected_instead_of_using_stale_candle(self):
        path = self.folder / "BTC_USDT-1h.feather"
        frame = pd.read_feather(path)
        frame = frame.drop(index=3).reset_index(drop=True)
        frame.to_feather(path)
        feed = HistoricalEventFeed(self.folder)
        with self.assertRaisesRegex(ValueError, "缺失或过期"):
            feed.at(START + timedelta(hours=4))

    def test_duplicate_timestamp_rejected(self):
        path = self.folder / "ETH_USDT-1h.feather"
        frame = pd.read_feather(path)
        frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
        frame.to_feather(path)
        with self.assertRaisesRegex(ValueError, "重复"):
            HistoricalEventFeed(self.folder)


if __name__ == "__main__":
    unittest.main()
