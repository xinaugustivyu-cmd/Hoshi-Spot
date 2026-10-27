"""预先登记的 B/C 单因素信号差异；全部基于已收盘 K 线。"""

from __future__ import annotations

import pandas as pd


def b_two_closed_exits(frame: pd.DataFrame) -> pd.Series:
    invalid = frame["trend_invalid"].fillna(False).astype(bool)
    return invalid & invalid.shift(1, fill_value=False)


def c_rising_4h_trend(frame: pd.DataFrame) -> pd.Series:
    return frame["trend_ok"].fillna(False) & (frame["ema20"] > frame["ema20"].shift(3))
