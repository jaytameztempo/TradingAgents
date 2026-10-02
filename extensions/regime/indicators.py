"""Moving average and Wilder's ADX on daily bars, in plain pandas and numpy."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(values: pd.Series, window: int) -> pd.Series:
    """Simple moving average; NaN until a full window of values exists."""
    return values.rolling(window, min_periods=window).mean()


def wilder_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average Directional Index as J. Welles Wilder defined it.

    True range and directional movement start on the second bar. Each is
    smoothed with Wilder's average (the first value is the mean of ``period``
    values, then ``prev + (x - prev) / period``). +DI and -DI come from the
    smoothed movement over smoothed true range, DX from their spread, and ADX
    is DX smoothed the same way. The first ADX is on bar ``2 * period - 1``
    (zero-based); earlier values are NaN.
    """
    high, low, close = (s.to_numpy(dtype=float) for s in (high, low, close))
    n = len(close)
    adx = np.full(n, np.nan)
    if n < 2 * period:
        return pd.Series(adx)

    prev_close = close[:-1]
    true_range = np.maximum.reduce(
        [high[1:] - low[1:], np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)]
    )
    up_move, down_move = high[1:] - high[:-1], low[:-1] - low[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    atr, plus, minus = (_wilder_average(x, period) for x in (true_range, plus_dm, minus_dm))
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = np.where(atr > 0, 100 * plus / atr, 0.0)
        minus_di = np.where(atr > 0, 100 * minus / atr, 0.0)
        di_sum = plus_di + minus_di
        dx = np.where(di_sum > 0, 100 * np.abs(plus_di - minus_di) / di_sum, 0.0)
    dx[np.isnan(atr)] = np.nan

    # dx[i] belongs to bar i + 1; its first valid value is at index period - 1.
    adx[1:] = _wilder_average(dx[period - 1:], period, pad=period - 1)
    return pd.Series(adx)


def _wilder_average(values: np.ndarray, period: int, pad: int = 0) -> np.ndarray:
    out = np.full(len(values) + pad, np.nan)
    if len(values) < period:
        return out
    current = values[:period].mean()
    out[pad + period - 1] = current
    for i in range(period, len(values)):
        current += (values[i] - current) / period
        out[pad + i] = current
    return out
