"""Wilder ATR and RSI, and confirmed ZigZag swings, with no market-data or Alpaca import.

They live here, apart from ``pivot`` (which fetches bars through ``alpaca_bars`` and so loads Alpaca's packages),
so the paper ticket step can use them while Alpaca's trading client is refused. ``pivot`` and the channel scans
import them from here. ``channel_lines`` is the channel scans' line rule: the last two confirmed swings of each kind.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

SWING_HIGH, SWING_LOW = "HIGH", "LOW"
SWING_REVERSAL_PCT = 5.0
ATR_PERIOD = RSI_PERIOD = 14
LINE_SWINGS = 2  # each channel line is set by the last two confirmed swings of its kind in the window


# --- indicators --------------------------------------------------------------

def _wilder(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's average: the first value is the mean of ``period`` values, then ``prev + (x - prev) / period``."""
    out = np.full(len(values), np.nan)
    if len(values) < period:
        return out
    current = values[:period].mean()
    out[period - 1] = current
    for i in range(period, len(values)):
        current += (values[i] - current) / period
        out[i] = current
    return out


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = ATR_PERIOD) -> pd.Series:
    """Average true range. True range starts on the second bar; the first ATR is on bar ``period``."""
    high, low, close = (s.to_numpy(dtype=float) for s in (high, low, close))
    out = np.full(len(close), np.nan)
    if len(close) > period:
        prev = close[:-1]
        true_range = np.maximum.reduce([high[1:] - low[1:], np.abs(high[1:] - prev), np.abs(low[1:] - prev)])
        out[1:] = _wilder(true_range, period)
    return pd.Series(out)


def wilder_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Relative strength index with Wilder's averages. No losses at all reads 100."""
    close = close.to_numpy(dtype=float)
    out = np.full(len(close), np.nan)
    if len(close) > period:
        change = np.diff(close)
        gain, loss = _wilder(np.clip(change, 0, None), period), _wilder(np.clip(-change, 0, None), period)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[1:] = np.where(loss > 0, 100 - 100 / (1 + gain / loss), np.where(np.isnan(loss), np.nan, 100.0))
    return pd.Series(out)


# --- swings ------------------------------------------------------------------

@dataclass(frozen=True)
class Swing:
    kind: str  # SWING_HIGH or SWING_LOW
    index: int  # bar of the extreme
    price: float  # the high for a swing high, the low for a swing low
    confirmed_index: int  # the bar whose close confirmed the reversal


def zigzag_swings(high, low, close, reversal_pct: float = SWING_REVERSAL_PCT) -> list[Swing]:
    """Alternating confirmed swings.

    A swing high is the highest high since the last swing low. It is confirmed by
    a later bar closing at least ``reversal_pct`` below it. A swing low is the
    mirror. The extreme still forming at the end of the bars is not returned.
    """
    high, low, close = (np.asarray(x, dtype=float) for x in (high, low, close))
    n = len(close)
    if n == 0:
        return []
    down_factor, up_factor = 1 - reversal_pct / 100, 1 + reversal_pct / 100
    swings: list[Swing] = []
    hi_i = lo_i = 0
    looking = None  # None until the first swing; then SWING_HIGH or SWING_LOW is the next one wanted
    for i in range(1, n):
        if looking in (None, SWING_HIGH) and high[i] > high[hi_i]:
            hi_i = i
        if looking in (None, SWING_LOW) and low[i] < low[lo_i]:
            lo_i = i
        top_confirmed = looking in (None, SWING_HIGH) and hi_i < i and close[i] <= high[hi_i] * down_factor
        bottom_confirmed = looking in (None, SWING_LOW) and lo_i < i and close[i] >= low[lo_i] * up_factor
        if looking is None and top_confirmed and bottom_confirmed:
            # Both ends of the opening range reversed on one bar: the later extreme is the swing.
            top_confirmed, bottom_confirmed = hi_i > lo_i, lo_i > hi_i
        if top_confirmed:
            swings.append(Swing(SWING_HIGH, hi_i, float(high[hi_i]), i))
            lo_i = hi_i + 1 + int(np.argmin(low[hi_i + 1:i + 1]))
            looking = SWING_LOW
        elif bottom_confirmed:
            swings.append(Swing(SWING_LOW, lo_i, float(low[lo_i]), i))
            hi_i = lo_i + 1 + int(np.argmax(high[lo_i + 1:i + 1]))
            looking = SWING_HIGH
    return swings


# --- channel lines -----------------------------------------------------------

def last_swings(swings: list, kind: str) -> list:
    """The last LINE_SWINGS swings of this kind, oldest first."""
    return sorted((s for s in swings if s.kind == kind), key=lambda s: s.index)[-LINE_SWINGS:]


def channel_lines(swings: list) -> tuple[float | None, float | None, list, list]:
    """(support, resistance, the swing lows that set support, the swing highs that set resistance) from the swings
    inside the window: support is the lower of the last two swing lows, resistance the higher of the last two
    swing highs. With only one swing of a kind, that swing sets the line; with none, the line is None."""
    low_pair, high_pair = last_swings(swings, SWING_LOW), last_swings(swings, SWING_HIGH)
    support = min(s.price for s in low_pair) if low_pair else None
    resistance = max(s.price for s in high_pair) if high_pair else None
    return support, resistance, low_pair, high_pair
