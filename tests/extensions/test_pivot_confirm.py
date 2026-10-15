"""Pivot confirmation for the channel scans: a channel pass stays a member if its daily close reclaimed the line in
the last 5 sessions, or the regular-session 4-hour bars printed a higher low (long) or lower high (short) after the
daily pivot. Bars after as_of are ignored, and only channel passes are read. No network.
"""

from datetime import date

import numpy as np
import pandas as pd

from extensions.scanbot.long_channel import ChannelCall
from extensions.scanbot.pivot import SWING_HIGH, SWING_LOW, Swing
from extensions.scanbot.pivot_confirm import (
    CLOSE_RECLAIM,
    FOUR_HOUR_TURN,
    LONG,
    SHORT,
    close_reclaim,
    confirm_filter,
    daily_pivot,
    four_hour_turn,
    recorded,
)

AS_OF = date(2026, 9, 30)
PIVOT_DAY = "2026-09-22"
CODE = "no_pivot_confirmation"

# Lows of regular-session 4-hour bars from the pivot day: the 99.5 low, a bounce, higher lows at 100.0, 100.3, 100.4.
HIGHER_LOWS = [99.5, 100.2, 100.8, 100.4, 100.0, 100.5, 100.9, 100.6, 100.3, 100.7, 101.0, 100.6, 100.4, 100.5]


def four_hour_frame(symbol, lows, start=PIVOT_DAY, highs=None, side=LONG):
    """Regular-session 4-hour bars, two a session (09:30 and 13:30) from ``start``. For a long ``lows`` are the
    lows and each high is 1 above unless ``highs`` is given; for a short the values are mirrored to highs at
    212 - low and lows at 212 - high."""
    days = pd.bdate_range(start=pd.Timestamp(start), periods=(len(lows) + 1) // 2)
    stamps = [d + pd.Timedelta(hours=h, minutes=30) for d in days for h in (9, 13)][:len(lows)]
    lows = np.array(lows, dtype=float)
    highs = lows + 1.0 if highs is None else np.array(highs, dtype=float)
    low, high = (lows, highs) if side == LONG else (212.0 - highs, 212.0 - lows)
    return pd.DataFrame({"symbol": symbol, "timestamp": pd.to_datetime(stamps),
                         "date": pd.to_datetime([s.normalize() for s in stamps]),
                         "open": (low + high) / 2, "high": high, "low": low, "close": (low + high) / 2,
                         "volume": 1000.0})


def _daily(lows, end=AS_OF, closes=None):
    lows = np.array(lows, dtype=float)
    closes = lows + 0.5 if closes is None else np.array(closes, dtype=float)
    return pd.DataFrame({"date": pd.bdate_range(end=pd.Timestamp(end), periods=len(lows)), "open": closes,
                         "high": lows + 1.0, "low": lows, "close": closes, "volume": 1e6})


# --- the daily pivot ---------------------------------------------------------

def test_long_pivot_is_the_lowest_low_after_the_last_swing_high_latest_on_ties():
    bars = _daily([100, 104, 110, 106, 101, 103, 101, 102])
    swings = [Swing(SWING_LOW, 0, 100.0, 2), Swing(SWING_HIGH, 2, 111.0, 4)]
    pivot = daily_pivot(bars, swings, LONG)
    assert pivot["price"] == 101.0 and pivot["date"] == bars["date"].iloc[6].date().isoformat()


def test_short_pivot_is_the_highest_high_after_the_last_swing_low():
    bars = _daily([110, 106, 100, 104, 108, 106, 107])
    swings = [Swing(SWING_HIGH, 0, 111.0, 2), Swing(SWING_LOW, 2, 100.0, 4)]
    pivot = daily_pivot(bars, swings, SHORT)
    assert pivot["price"] == 109.0 and pivot["date"] == bars["date"].iloc[4].date().isoformat()


def test_no_pivot_without_an_opposite_swing():
    bars = _daily([100, 101, 102])
    assert daily_pivot(bars, [Swing(SWING_LOW, 0, 100.0, 2)], LONG) is None
    assert daily_pivot(bars, [Swing(SWING_HIGH, 2, 103.0, 2)], LONG) is None  # no bar after the swing


# --- close reclaim -----------------------------------------------------------

def test_long_reclaim_needs_a_tag_in_5_sessions_and_a_close_back_above():
    support = 100.0
    tagged_today = _daily([101, 101, 101, 101, 101, 99.8], closes=[101.5] * 5 + [100.3])
    assert close_reclaim(tagged_today, support, LONG)["passed"]
    five_back = _daily([101, 100.0, 101, 101, 101, 101], closes=[101.5] * 6)
    result = close_reclaim(five_back, support, LONG)
    assert result["passed"] and result["tag_date"] == five_back["date"].iloc[1].date().isoformat()
    six_back = _daily([100.0, 101, 101, 101, 101, 101], closes=[101.5] * 6)
    assert not close_reclaim(six_back, support, LONG)["passed"]


def test_long_reclaim_fails_when_the_close_is_not_above_support():
    at_line = _daily([101, 101, 101, 101, 99.5], closes=[101.5] * 4 + [100.0])
    lost = _daily([101, 101, 99.5, 101, 99.5], closes=[101.5, 101.5, 100.5, 101.5, 99.8])
    assert not close_reclaim(at_line, 100.0, LONG)["passed"]
    result = close_reclaim(lost, 100.0, LONG)
    assert not result["passed"] and "not above" in result["detail"]


def test_short_reclaim_mirrors_on_resistance():
    bars = _daily([108, 108, 108, 108, 109.5], closes=[108.5] * 4 + [109.7])  # highs 109, last high 110.5
    assert close_reclaim(bars, 110.0, SHORT)["passed"]
    assert not close_reclaim(bars, 110.6, SHORT)["passed"]  # no tag
    above = _daily([108, 108, 108, 108, 109.5], closes=[108.5] * 4 + [110.2])
    assert not close_reclaim(above, 110.0, SHORT)["passed"]


def test_no_line_is_no_reclaim():
    assert not close_reclaim(_daily([100, 101]), None, LONG)["passed"]


# --- the 4-hour turn ---------------------------------------------------------

def test_long_higher_low_after_a_bounce_passes():
    result = four_hour_turn(four_hour_frame("A", HIGHER_LOWS), PIVOT_DAY, LONG)
    assert result["passed"]
    assert result["anchor_price"] == 99.5 and result["turn_price"] == 100.4
    assert result["turn_bar"] == "2026-09-30 09:30"


def test_short_lower_high_mirrors():
    result = four_hour_turn(four_hour_frame("A", HIGHER_LOWS, side=SHORT), PIVOT_DAY, SHORT)
    assert result["passed"]
    assert result["anchor_price"] == 112.5 and result["turn_price"] == 111.6


def test_an_equal_low_is_not_a_higher_low():
    lows = [99.5, 100.5, 101.0, 99.5, 100.2, 100.6]
    assert not four_hour_turn(four_hour_frame("A", lows), PIVOT_DAY, LONG)["passed"]


def test_a_wiggle_without_a_bounce_is_not_a_higher_low():
    # The bar between the low and the dip never trades above the low bar's high (100.5).
    lows = [99.5, 99.8, 99.7, 99.9]
    result = four_hour_turn(four_hour_frame("A", lows, highs=[100.5, 100.4, 100.3, 100.6]), PIVOT_DAY, LONG)
    assert not result["passed"]
    assert four_hour_turn(four_hour_frame("A", lows, highs=[100.5, 100.7, 100.3, 100.6]), PIVOT_DAY, LONG)["passed"]


def test_the_latest_bar_cannot_confirm_itself():
    assert not four_hour_turn(four_hour_frame("A", [99.5, 100.6, 101.0, 100.2]), PIVOT_DAY, LONG)["passed"]


def test_a_broken_higher_low_fails():
    result = four_hour_turn(four_hour_frame("A", [99.5, 100.6, 100.2, 100.7, 100.1]), PIVOT_DAY, LONG)
    assert not result["passed"] and "broken" in result["detail"]


def test_a_pivot_on_the_last_bars_and_no_bars_fail():
    assert not four_hour_turn(four_hour_frame("A", [100.0, 99.5]), PIVOT_DAY, LONG)["passed"]
    empty = four_hour_frame("A", [])
    assert "no regular-session 4-hour bars" in four_hour_turn(empty, PIVOT_DAY, LONG)["detail"]


def test_bars_before_the_pivot_day_are_not_read():
    before = four_hour_frame("A", [95.0, 96.0], start="2026-09-21")
    bars = pd.concat([before, four_hour_frame("A", HIGHER_LOWS)], ignore_index=True)
    assert four_hour_turn(bars, PIVOT_DAY, LONG)["anchor_price"] == 99.5


# --- the scan step -----------------------------------------------------------

class FakeFourHour:
    def __init__(self, frames):
        self.frames = frames
        self.requested = []

    def __call__(self, symbols, start, end, *, feed, cache_dir=None):
        self.requested.append((list(symbols), start, end, feed))
        frames = [self.frames[s] for s in symbols if s in self.frames]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _call(passed, reclaim_passed, pivot_date=PIVOT_DAY):
    confirmation = {
        "pivot": {"date": pivot_date, "price": 99.5} if pivot_date else None,
        CLOSE_RECLAIM: {"passed": reclaim_passed, "detail": "reclaim " + ("yes" if reclaim_passed else "no")},
        FOUR_HOUR_TURN: None, "passed_by": None,
    }
    return ChannelCall(passed, [] if passed else ["adx_above_20"], ["channel detail"], {}, 0,
                       {"pivot_confirmation": confirmation})


def test_confirm_filter_keeps_either_and_records_a_double_miss():
    later = four_hour_frame("TURN", [90.0, 91.0], start="2026-10-01")  # after as_of: would break the higher low
    fetcher = FakeFourHour({"TURN": pd.concat([four_hour_frame("TURN", HIGHER_LOWS), later], ignore_index=True),
                            "BOTH": four_hour_frame("BOTH", HIGHER_LOWS)})
    calls = {"RECL": _call(True, True), "TURN": _call(True, False), "BOTH": _call(True, True),
             "NONE": _call(True, False), "NOPIV": _call(True, False, pivot_date=None), "FAIL": _call(False, True)}
    out = confirm_filter(calls, LONG, CODE, AS_OF, fetcher, None)

    assert fetcher.requested == [(["BOTH", "NONE", "NOPIV", "RECL", "TURN"], date(2026, 6, 2), AS_OF, "sip")]
    assert out["FAIL"] is calls["FAIL"]
    assert out["RECL"].passed and out["RECL"].metrics["pivot_confirmation"]["passed_by"] == [CLOSE_RECLAIM]
    assert out["TURN"].passed and out["TURN"].metrics["pivot_confirmation"]["passed_by"] == [FOUR_HOUR_TURN]
    assert out["BOTH"].metrics["pivot_confirmation"]["passed_by"] == [CLOSE_RECLAIM, FOUR_HOUR_TURN]
    assert out["TURN"].detail[0] == "channel detail" and "4-hour higher low" in out["TURN"].detail[-1]
    for name in ("NONE", "NOPIV"):
        assert not out[name].passed and out[name].reasons == [CODE]
        assert out[name].detail[0] == "reclaim no" and out[name].detail[1].startswith("no 4-hour higher low")
        assert out[name].metrics["pivot_confirmation"]["passed_by"] == []


def test_confirm_filter_reads_nothing_without_channel_passes():
    fetcher = FakeFourHour({})
    calls = {"FAIL": _call(False, True)}
    assert confirm_filter(calls, LONG, CODE, AS_OF, fetcher, None) is calls
    assert fetcher.requested == []


def test_recorded_leaves_the_4hour_turn_for_the_filter():
    bars = _daily([101, 104, 110, 106, 100.0], closes=[101.5, 104.5, 110.5, 106.5, 100.5])
    out = recorded(bars, [Swing(SWING_HIGH, 2, 111.0, 4)], 100.0, LONG)
    assert out["pivot"]["price"] == 100.0 and out[CLOSE_RECLAIM]["passed"]
    assert out[FOUR_HOUR_TURN] is None and out["passed_by"] is None
