import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "user_data/strategies"))

from HoshiSpotV11A import HoshiSpotV11A
from HoshiSpotV11B import HoshiSpotV11B
from HoshiSpotV11C import HoshiSpotV11C
from hoshi.indicators import merge_closed_informative
from hoshi_v11.variants import b_two_closed_exits, c_rising_4h_trend


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)


class ExperimentVariantTests(unittest.TestCase):
    def test_a_b_c_share_identical_policy_hash(self):
        self.assertEqual({HoshiSpotV11A.risk_policy.hash, HoshiSpotV11B.risk_policy.hash,
                          HoshiSpotV11C.risk_policy.hash}, {HoshiSpotV11A.risk_policy.hash})
        self.assertEqual({HoshiSpotV11A.account_id, HoshiSpotV11B.account_id,
                          HoshiSpotV11C.account_id}, {"A", "B", "C"})

    def test_b_requires_two_closed_invalid_1h_candles(self):
        frame = pd.DataFrame({"trend_invalid": [False, True, False, True, True, False]})
        self.assertEqual(b_two_closed_exits(frame).tolist(), [False, False, False, False, True, False])
        strategy = HoshiSpotV11B.__new__(HoshiSpotV11B)
        filled = strategy.populate_exit_trend(frame, {})
        self.assertEqual(filled["exit_long"].tolist(), [0, 0, 0, 0, 1, 0])

    def test_b_custom_exit_matches_vectorized_path_and_preserves_14d(self):
        strategy = HoshiSpotV11B.__new__(HoshiSpotV11B)
        now = START + timedelta(hours=3)
        frame = pd.DataFrame({
            "date": pd.to_datetime([START, START + timedelta(hours=1), START + timedelta(hours=2)], utc=True),
            "trend_invalid": [False, True, True],
        })
        strategy.dp = SimpleNamespace(get_analyzed_dataframe=lambda pair, timeframe: (frame, now))
        trade = SimpleNamespace(open_date_utc=START)
        self.assertEqual(strategy.custom_exit("BTC/USDT", trade, now, 100, -0.01),
                         "trend_invalid_two_closed_1h")
        frame.loc[1, "trend_invalid"] = False
        self.assertIsNone(strategy.custom_exit("BTC/USDT", trade, now, 100, -0.01))
        self.assertEqual(strategy.custom_exit("BTC/USDT", trade,
                                             START + timedelta(days=14), 100, -0.01),
                         "max_holding_14d")

    def test_c_requires_4h_ema20_above_three_closed_candles_ago(self):
        frame = pd.DataFrame({"trend_ok": [True] * 6, "ema20": [10, 11, 12, 13, 12, 15]})
        self.assertEqual(c_rising_4h_trend(frame).tolist(),
                         [False, False, False, True, True, True])

    def test_c_4h_trend_stays_hidden_before_candle_close(self):
        base = pd.DataFrame({
            "date": pd.to_datetime([START + timedelta(hours=h) for h in (3, 4, 7, 8)], utc=True),
            "close": [1, 2, 3, 4],
        })
        higher = pd.DataFrame({
            "date": pd.to_datetime([START, START + timedelta(hours=4)], utc=True),
            "trend_ok": [False, True],
        })
        merged = merge_closed_informative(base, higher)
        self.assertTrue(pd.isna(merged.iloc[0]["trend_ok_informative"]))
        self.assertFalse(bool(merged.iloc[1]["trend_ok_informative"]))
        self.assertFalse(bool(merged.iloc[2]["trend_ok_informative"]))
        self.assertTrue(bool(merged.iloc[3]["trend_ok_informative"]))

    def test_c_future_4h_rows_do_not_change_past_indicator_prefix(self):
        strategy = HoshiSpotV11C.__new__(HoshiSpotV11C)
        strategy.config = {"strategy_parameters": {
            "ema_fast": 20, "ema_slow": 50,
        }}
        close = [100 + i * 0.4 for i in range(80)]
        base = pd.DataFrame({"close": close})
        longer = pd.concat([base, pd.DataFrame({"close": [30, 40, 50]})], ignore_index=True)
        first = strategy.populate_indicators_4h(base, {})
        second = strategy.populate_indicators_4h(longer, {}).iloc[:len(base)]
        pd.testing.assert_series_equal(first["ema20"], second["ema20"])
        pd.testing.assert_series_equal(first["trend_ok"], second["trend_ok"])


if __name__ == "__main__":
    unittest.main()
