"""小而可测的 OHLCV 指标和 Hoshi Spot V1 信号规则。"""

from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    average_gain = gains.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    average_loss = losses.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    relative_strength = average_gain / average_loss.where(average_loss.ne(0))
    result = 100 - (100 / (1 + relative_strength))
    return result.mask(average_loss.eq(0) & average_gain.gt(0), 100.0)


def add_1h_indicators(frame: pd.DataFrame, params: dict) -> pd.DataFrame:
    result = frame.copy()
    result["ema20"] = ema(result["close"], int(params["ema_fast"]))
    result["rsi14"] = rsi(result["close"], int(params["rsi_period"]))
    result["volume_mean_prev20"] = (
        result["volume"].rolling(int(params["volume_period"]), min_periods=int(params["volume_period"]))
        .mean()
        .shift(1)
    )
    result["candle_change"] = result["close"] / result["open"] - 1
    result["rsi_cross_up_50"] = (result["rsi14"].shift(1) <= 50) & (result["rsi14"] > 50)
    result["close_cross_up_ema20"] = (result["close"].shift(1) <= result["ema20"].shift(1)) & (
        result["close"] > result["ema20"]
    )
    result["trend_invalid"] = (result["close"] < result["ema20"]) & (result["rsi14"] < 45)
    return result


def add_4h_indicators(frame: pd.DataFrame, params: dict) -> pd.DataFrame:
    result = frame.copy()
    result["ema20"] = ema(result["close"], int(params["ema_fast"]))
    result["ema50"] = ema(result["close"], int(params["ema_slow"]))
    result["trend_ok"] = (result["ema20"] > result["ema50"]) & (result["close"] > result["ema50"])
    return result


def merge_closed_informative(
    base: pd.DataFrame, informative: pd.DataFrame, base_minutes: int = 60, informative_minutes: int = 240
) -> pd.DataFrame:
    """按 K 线收盘时间合并高周期数据；高周期 candle 在收盘前不可见。"""
    if informative_minutes < base_minutes:
        raise ValueError("informative 周期不得小于主周期")
    left = base.copy()
    right = informative.copy()
    left["date"] = pd.to_datetime(left["date"], utc=True)
    right["date"] = pd.to_datetime(right["date"], utc=True) + pd.Timedelta(minutes=informative_minutes)
    right = right.rename(columns={column: f"{column}_informative" for column in right.columns if column != "date"})
    return pd.merge_asof(left.sort_values("date"), right.sort_values("date"), on="date", direction="backward")


def entry_mask(frame: pd.DataFrame, params: dict) -> pd.Series:
    """全部入场条件必须同时成立；4h 行必须已按 Freqtrade informative 规则闭合合并。"""
    return (
        frame["trend_ok_4h"].fillna(False)
        & frame["rsi_cross_up_50"].fillna(False)
        & (frame["rsi14"] <= float(params["rsi_max"]))
        & frame["close_cross_up_ema20"].fillna(False)
        & (frame["volume"] > frame["volume_mean_prev20"] * float(params["volume_multiplier"]))
        & (frame["candle_change"] <= float(params["max_candle_gain"]))
        & (frame["volume"] > 0)
    )


def exit_mask(frame: pd.DataFrame) -> pd.Series:
    return frame["trend_invalid"].fillna(False)

