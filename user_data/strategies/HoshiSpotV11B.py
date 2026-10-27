"""B 组：V1 原始入场，趋势失效需连续两根已收盘 1h 确认。"""

import sys
from datetime import timedelta, timezone
from pathlib import Path

_USER_DATA = str(Path(__file__).resolve().parents[1])
if _USER_DATA not in sys.path:
    sys.path.insert(0, _USER_DATA)

from pandas import DataFrame
from freqtrade.persistence import Trade
from hoshi_v11.freqtrade_strategy import HoshiV11Base
from hoshi_v11.variants import b_two_closed_exits


class HoshiSpotV11B(HoshiV11Base):
    account_id = "B"

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        result = dataframe.copy()
        result["exit_long"] = 0
        result["exit_tag"] = None
        mask = b_two_closed_exits(result)
        result.loc[mask, "exit_long"] = 1
        result.loc[mask, "exit_tag"] = "trend_invalid_two_closed_1h"
        return result

    def custom_exit(self, pair: str, trade: Trade, current_time,
                    current_rate: float, current_profit: float, **kwargs) -> str | None:
        if current_time - trade.open_date_utc >= timedelta(days=14):
            return "max_holding_14d"
        frame, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if frame is None or len(frame) < 2:
            return None
        last_start = frame.iloc[-1]["date"].to_pydatetime()
        if last_start.tzinfo is None:
            last_start = last_start.replace(tzinfo=timezone.utc)
        if last_start + timedelta(hours=1) > current_time:
            return None
        if bool(b_two_closed_exits(frame).iloc[-1]):
            return "trend_invalid_two_closed_1h"
        return None
