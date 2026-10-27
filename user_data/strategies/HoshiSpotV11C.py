"""C 组：V1 原始出场，仅增加已收盘 4h EMA20 的三根上升筛选。"""

import sys
from pathlib import Path

_USER_DATA = str(Path(__file__).resolve().parents[1])
if _USER_DATA not in sys.path:
    sys.path.insert(0, _USER_DATA)

from pandas import DataFrame
from freqtrade.strategy import informative
from hoshi.indicators import add_4h_indicators
from hoshi_v11.freqtrade_strategy import HoshiV11Base
from hoshi_v11.variants import c_rising_4h_trend


class HoshiSpotV11C(HoshiV11Base):
    account_id = "C"

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        result = add_4h_indicators(dataframe, self._params())
        result["trend_ok"] = c_rising_4h_trend(result)
        return result
