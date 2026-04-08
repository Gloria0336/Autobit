import pandas as pd
import numpy as np
from config import (
    EMA_TREND_PERIOD, EMA_SIGNAL_PERIOD,
    RSI_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL_LINE
)


def ema(series: pd.Series, period: int) -> pd.Series:
    """
    指數移動平均（EMA）。
    公式：EMA_t = close_t * k + EMA_{t-1} * (1 - k)，k = 2 / (period + 1)
    """
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """
    相對強弱指數（RSI），使用 Wilder 平滑法。
    公式：RSI = 100 - 100 / (1 + avg_gain / avg_loss)
    """
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_series = 100 - (100 / (1 + rs))
    rsi_series = rsi_series.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    rsi_series = rsi_series.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return rsi_series


def macd(
    series: pd.Series,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL_LINE,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    MACD 指標。
    回傳：(macd_line, signal_line, histogram)
    histogram > 0 代表多頭動能；由正轉負代表動能耗竭。
    """
    macd_line   = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_all(
    trend_df: pd.DataFrame,
    signal_df: pd.DataFrame,
    *,
    ema_trend_period: int = EMA_TREND_PERIOD,
    ema_signal_period: int = EMA_SIGNAL_PERIOD,
    rsi_period: int = RSI_PERIOD,
    macd_fast: int = MACD_FAST,
    macd_slow: int = MACD_SLOW,
    macd_signal: int = MACD_SIGNAL_LINE,
) -> dict:
    """
    統一計算所有指標，回傳最新數值字典：
      ema200        - 1h EMA200 最新值
      ema20         - 15m EMA20 最新值
      rsi           - 15m RSI(14) 最新值
      macd_hist     - 15m MACD 柱狀圖最新值
      macd_hist_prev- 15m MACD 柱狀圖前一根值（用於判斷翻轉）
    """
    close_trend  = trend_df["close"]
    close_signal = signal_df["close"]

    ema200_series = ema(close_trend, ema_trend_period)
    ema20_series  = ema(close_signal, ema_signal_period)
    rsi_series    = rsi(close_signal, rsi_period)
    _, _, hist    = macd(close_signal, macd_fast, macd_slow, macd_signal)

    return {
        "ema200":         float(ema200_series.iloc[-1]),
        "ema20":          float(ema20_series.iloc[-1]),
        "rsi":            float(rsi_series.iloc[-1]),
        "macd_hist":      float(hist.iloc[-1]),
        "macd_hist_prev": float(hist.iloc[-2]) if len(hist) >= 2 else 0.0,
    }
