"""历史公共行情事件。按决策时点截取已收盘 1h/4h，供 A/B/C 重放。"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .event_model import MarketEvent, event_digest

class HistoricalEventFeed:
    def __init__(self, data_dir: str | Path, pairs: tuple[str, ...] = ("BTC/USDT", "ETH/USDT")):
        self.frames = {}
        self.times = {}
        for pair in pairs:
            for timeframe, delta in (("1h", timedelta(hours=1)), ("4h", timedelta(hours=4))):
                path = Path(data_dir) / f"{pair.replace('/', '_')}-{timeframe}.feather"
                frame = pd.read_feather(path)
                if frame.empty or frame["date"].isna().any() or frame["date"].duplicated().any():
                    raise ValueError(f"历史行情缺失或重复：{path}")
                frame = frame.sort_values("date", kind="stable").reset_index(drop=True)
                starts = pd.to_datetime(frame["date"], utc=True).dt.to_pydatetime().tolist()
                if any(b <= a for a, b in zip(starts, starts[1:])):
                    raise ValueError(f"历史行情乱序：{path}")
                key = (pair, timeframe)
                self.frames[key] = frame
                self.times[key] = [value + delta for value in starts]
        self.pairs = pairs

    def at(self, decision_time: datetime) -> MarketEvent:
        if decision_time.tzinfo is None or decision_time.utcoffset() is None:
            raise ValueError("决策时间必须含时区")
        now = decision_time.astimezone(timezone.utc)
        data: dict[str, dict[str, dict]] = {}
        for pair in self.pairs:
            data[pair] = {}
            for timeframe, max_age in (("1h", timedelta(0)), ("4h", timedelta(hours=3))):
                key = (pair, timeframe)
                closes = self.times[key]
                index = bisect_right(closes, now) - 1
                if index < 0 or now - closes[index] > max_age:
                    raise ValueError(f"{pair} {timeframe} 已收盘行情缺失或过期：{now.isoformat()}")
                row = self.frames[key].iloc[index]
                data[pair][timeframe] = {
                    "close_time_utc": closes[index].isoformat(),
                    **{name: float(row[name]) for name in ("open", "high", "low", "close", "volume")},
                }
        event_id = event_digest(now, data)
        return MarketEvent(event_id, now, now, data, None)
