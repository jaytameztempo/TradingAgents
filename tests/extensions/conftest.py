"""Fixtures shared by the extensions tests."""

import numpy as np
import pandas as pd
import pytest

from paper_orders.session_bars import THIRTY_MINUTE_COLUMNS


def thirty_minute_bars(symbol, as_of, step=0.5, sessions=60, ramp_sessions=2, ramp_end=None, base=100.0,
                       extended_hours=False, tail=None):
    """Regular-session 30-minute bars (09:30 ... 15:30 New York) on the weekdays ending at as_of: a quiet range
    (closes within 5 cents, bars $2 high to low, so the squeeze is on), then ``ramp_sessions`` sessions moving
    ``step`` a bar, ending on the session ``ramp_end`` (as_of by default). step > 0 fires the TTM Squeeze bullish on
    both the 4-hour and the 1-hour bars, step < 0 bearish. tail = (n, tail_step) moves the last n bars tail_step
    each instead. With extended_hours, an 08:00 and a 16:00 bar far from the price are added each day."""
    days = pd.bdate_range(end=pd.Timestamp(as_of), periods=sessions)
    last = len(days) - 1 if ramp_end is None else days.get_loc(pd.Timestamp(ramp_end))
    rows, close, i = [], base, 0
    tail_from = len(days) * 13 - tail[0] if tail else None
    for d_i, day in enumerate(days):
        for k in range(13):
            if tail and i >= tail_from:
                close += tail[1]
            elif last - ramp_sessions < d_i <= last:
                close += step
            c = close + 0.05 * np.sin(i)
            ts = day + pd.Timedelta(hours=9, minutes=30 + 30 * k)
            rows.append((symbol, ts, c, c + 1, c - 1, c, 1000.0))
            i += 1
        if extended_hours:
            for at in (pd.Timedelta(hours=8), pd.Timedelta(hours=16)):
                rows.append((symbol, day + at, 500.0, 900.0, 1.0, 500.0, 1.0))
    return pd.DataFrame(rows, columns=THIRTY_MINUTE_COLUMNS).sort_values("timestamp").reset_index(drop=True)


@pytest.fixture
def squeeze_bars():
    """thirty_minute_bars, for tests that need bars which fire (or not) the TTM Squeeze."""
    return thirty_minute_bars
