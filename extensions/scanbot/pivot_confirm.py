"""Pivot confirmation for the channel scans (``long_channel`` and ``short_channel``).

A channel pass stays a member only if it also passes at least one of two confirmations on bars dated on or before
as_of. A name that fails both carries the scan's ``no_pivot_confirmation`` code, with one detail line per miss.

The daily pivot is the extreme still forming at the channel line: for a long, the lowest daily low after the last
confirmed ZigZag swing high (the latest bar on equal lows); for a short, the highest daily high after the last
confirmed swing low. It need not be confirmed by the 5% reversal.

Close reclaim, on the daily bars the scan already fetched:

- long: a bar in the last ``RECLAIM_SESSIONS`` (5) sessions, as_of included, has a low at or below support, and the
  as-of close is strictly above support. A reclaim that has since been lost fails.
- short: a bar in that window has a high at or above resistance, and the as-of close is strictly below it.

4-hour turn, on regular-session 4-hour bars (09:30-13:30 and 13:30-16:00 New York, built from 30-minute bars by
``fetch_4hour_bars``, no extended hours) dated from the pivot day through as_of. They are fetched only for names
that already passed the channel rules. For a long:

- anchor: the 4-hour bar with the lowest low (the latest bar on equal lows), low L,
- higher low: a later bar q with low[q] > L, low[q] < low[q-1] and low[q] <= low[q+1]; the bar after q must exist,
  so the latest bar cannot confirm itself,
- bounce first: a bar strictly between the anchor and q has a high above the anchor bar's high,
- intact: the latest such q is used, and no later bar's low is below low[q].

A short is the mirror: a lower high under the anchor's highest high, after a bar whose low is below the anchor
bar's low, not exceeded since.

Nothing here writes a file or places an order. The only read is the 4-hour fetcher the caller passes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from extensions.scanbot.funnel import _fetch_in_chunks
from extensions.scanbot.pivot import SWING_HIGH, SWING_LOW, Swing

LONG, SHORT = "LONG", "SHORT"  # the scans' PIVOT_SIDE
RECLAIM_SESSIONS = 5
FOUR_HOUR_LOOKBACK_DAYS = 120  # calendar days of 4-hour bars before as_of; the pivot is inside the 60-session window
CLOSE_RECLAIM, FOUR_HOUR_TURN = "close_reclaim", "four_hour_turn"

LINE = {LONG: "support", SHORT: "resistance"}
TURN = {LONG: "higher low", SHORT: "lower high"}


def rules(side: str) -> dict:
    """The confirmation rules for a scan's policy."""
    line = LINE[side]
    if side == LONG:
        pivot = "lowest daily low after the last confirmed swing high; the latest bar on equal lows"
        reclaim = (f"a daily low at or below support in the last {RECLAIM_SESSIONS} sessions, and the as-of close "
                   "strictly above support")
        turn = ("on regular-session 4-hour bars from the pivot day: after the lowest 4-hour low L, a bounce above "
                "that bar's high, then a 4-hour swing low above L (lower than the bar before, not above the bar "
                "after) that no later low has undercut")
    else:
        pivot = "highest daily high after the last confirmed swing low; the latest bar on equal highs"
        reclaim = (f"a daily high at or above resistance in the last {RECLAIM_SESSIONS} sessions, and the as-of "
                   "close strictly below resistance")
        turn = ("on regular-session 4-hour bars from the pivot day: after the highest 4-hour high H, a drop below "
                "that bar's low, then a 4-hour swing high below H (higher than the bar before, not below the bar "
                "after) that no later high has exceeded")
    return {
        "pivot_confirmation": f"keep a channel pass only if it passes the close reclaim or the 4-hour {TURN[side]}; "
                              "either one is enough",
        "daily_pivot": pivot,
        "close_reclaim": reclaim,
        "close_reclaim_line": line,
        "reclaim_sessions": RECLAIM_SESSIONS,
        "four_hour_turn": turn,
        "four_hour_bars": "regular_session_4h_from_30min, channel passes only",
        "four_hour_lookback_calendar_days": FOUR_HOUR_LOOKBACK_DAYS,
    }


# --- the daily bars ----------------------------------------------------------

def daily_pivot(bars: pd.DataFrame, swings: list[Swing], side: str) -> dict | None:
    """The forming extreme after the last confirmed opposite swing. None when there is no such swing or bar.

    ``bars`` is the scan's sorted daily frame with a 0..n-1 index; ``swings`` its ZigZag swings."""
    opposite = SWING_HIGH if side == LONG else SWING_LOW
    before = [s for s in swings if s.kind == opposite]
    if not before or before[-1].index + 1 >= len(bars):
        return None
    values = bars["low" if side == LONG else "high"].iloc[before[-1].index + 1:].to_numpy(dtype=float)
    extreme = values.min() if side == LONG else values.max()
    at = before[-1].index + 1 + int(np.flatnonzero(values == extreme)[-1])
    return {"date": bars["date"].iloc[at].date().isoformat(), "price": _round(extreme),
            "after_swing": bars["date"].iloc[before[-1].index].date().isoformat()}


def close_reclaim(bars: pd.DataFrame, line: float | None, side: str) -> dict:
    """The daily close reclaim of the channel line. ``bars`` is sorted and ends on the as-of bar."""
    word = LINE[side]
    out = {"passed": False, "line": _round(line), "sessions": RECLAIM_SESSIONS, "tag_date": None, "tag_price": None,
           "close": _round(float(bars["close"].iloc[-1])) if len(bars) else None}
    if line is None or bars.empty:
        return {**out, "detail": f"no close reclaim: no {word} to reclaim"}
    recent = bars.tail(RECLAIM_SESSIONS)
    tagged = recent[recent["low"] <= line] if side == LONG else recent[recent["high"] >= line]
    last_close = float(bars["close"].iloc[-1])
    edge, at_word, back = ("low", "at or below", "above") if side == LONG else ("high", "at or above", "below")
    if tagged.empty:
        return {**out, "detail": f"no close reclaim: no daily {edge} {at_word} {word} {line:.2f} in the last "
                                 f"{RECLAIM_SESSIONS} sessions"}
    tag = tagged.iloc[-1]
    out.update(tag_date=tag["date"].date().isoformat(), tag_price=_round(float(tag[edge])))
    if not (last_close > line if side == LONG else last_close < line):
        return {**out, "detail": f"no close reclaim: {word} {line:.2f} tagged on {out['tag_date']}, but the as-of "
                                 f"close {last_close:.2f} is not {back} it"}
    return {**out, "passed": True,
            "detail": f"close reclaim: daily {edge} {tag[edge]:.2f} on {out['tag_date']} tagged {word} {line:.2f}; "
                      f"the as-of close {last_close:.2f} is back {back} it"}


# --- the 4-hour bars ---------------------------------------------------------

def four_hour_turn(bars_4h: pd.DataFrame, pivot_date: str, side: str) -> dict:
    """The 4-hour higher low (long) or lower high (short) after the daily pivot. The caller has dropped bars after
    as_of."""
    turn, word = TURN[side], ("low" if side == LONG else "high")
    bars = bars_4h[bars_4h["date"] >= pd.Timestamp(pivot_date)].sort_values("timestamp").reset_index(drop=True)
    out = {"passed": False, "pivot_date": pivot_date, "bars_4h": len(bars), "anchor_bar": None, "anchor_price": None,
           "turn_bar": None, "turn_price": None}
    if bars.empty:
        return {**out, "detail": f"no 4-hour {turn}: no regular-session 4-hour bars from the {pivot_date} pivot "
                                 "to as_of"}

    # Work in "long" terms: for a short, negate so the highest high is the lowest value.
    sign = 1.0 if side == LONG else -1.0
    ext = sign * bars["low" if side == LONG else "high"].to_numpy(dtype=float)
    opp = sign * bars["high" if side == LONG else "low"].to_numpy(dtype=float)
    n = len(bars)
    a = int(np.flatnonzero(ext == ext.min())[-1])
    stamp = lambda i: bars["timestamp"].iloc[i].strftime("%Y-%m-%d %H:%M")  # noqa: E731
    out.update(anchor_bar=stamp(a), anchor_price=_round(sign * ext[a]))

    turns = [q for q in range(a + 2, n - 1)
             if ext[q] > ext[a] and ext[q] < ext[q - 1] and ext[q] <= ext[q + 1] and opp[a + 1:q].max() > opp[a]]
    if not turns:
        return {**out, "detail": f"no 4-hour {turn}: the {sign * ext[a]:.2f} {word} at {stamp(a)} has no confirmed "
                                 f"{turn} after it ({n} 4-hour bars from the {pivot_date} pivot)"}
    q = turns[-1]
    out.update(turn_bar=stamp(q), turn_price=_round(sign * ext[q]))
    later = ext[q + 1:]
    if (later < ext[q]).any():
        broke = q + 1 + int(np.flatnonzero(later < ext[q])[0])
        return {**out, "detail": f"no 4-hour {turn}: the {turn} {sign * ext[q]:.2f} at {stamp(q)} was broken at "
                                 f"{stamp(broke)}"}
    side_word = "above" if side == LONG else "below"
    return {**out, "passed": True,
            "detail": f"4-hour {turn} {sign * ext[q]:.2f} at {stamp(q)}, {side_word} the {sign * ext[a]:.2f} {word} "
                      f"at {stamp(a)} after the {pivot_date} daily pivot"}


# --- the scan step -----------------------------------------------------------

def confirm_filter(calls: dict, side: str, code: str, as_of: date, four_hour_fetcher: Callable,
                   cache_dir: str | Path | None) -> dict:
    """Confirm the names that passed the channel rules. ``classify`` recorded each one's daily pivot and close reclaim
    under metrics["pivot_confirmation"]; this fetches the passes' 4-hour bars, adds the 4-hour turn and keeps a name
    that passes either. A name that fails both is no longer a pass: it carries ``code`` and both misses. Names that
    failed the channel rules are not read and come back unchanged."""
    passed = sorted(s for s, c in calls.items() if c.passed)
    if not passed:
        return calls
    start = as_of - timedelta(days=FOUR_HOUR_LOOKBACK_DAYS)
    bars = _fetch_in_chunks(four_hour_fetcher, passed, start, as_of, cache_dir)
    groups = dict(tuple(bars[bars["date"] <= pd.Timestamp(as_of)].groupby("symbol"))) if not bars.empty else {}
    empty = pd.DataFrame({"symbol": [], "timestamp": pd.to_datetime([]), "date": pd.to_datetime([]),
                          "open": [], "high": [], "low": [], "close": []})

    out = dict(calls)
    for symbol in passed:
        call = calls[symbol]
        confirmation = call.metrics["pivot_confirmation"]
        pivot, reclaim = confirmation["pivot"], confirmation[CLOSE_RECLAIM]
        if pivot is None:
            turn = {"passed": False, "pivot_date": None, "bars_4h": 0,
                    "detail": f"no 4-hour {TURN[side]}: no daily pivot to measure from"}
        else:
            turn = four_hour_turn(groups.get(symbol, empty), pivot["date"], side)
        passed_by = [name for name, check in ((CLOSE_RECLAIM, reclaim), (FOUR_HOUR_TURN, turn)) if check["passed"]]
        metrics = {**call.metrics,
                   "pivot_confirmation": {**confirmation, FOUR_HOUR_TURN: turn, "passed_by": passed_by}}
        if passed_by:
            confirmed = [check["detail"] for check in (reclaim, turn) if check["passed"]]
            out[symbol] = replace(call, detail=call.detail + confirmed, metrics=metrics)
        else:
            out[symbol] = replace(call, passed=False, reasons=[code], detail=[reclaim["detail"], turn["detail"]],
                                  metrics=metrics)
    return out


def recorded(bars: pd.DataFrame, swings: list[Swing], line: float | None, side: str) -> dict:
    """What ``classify`` records for a measured name: the daily pivot and the close reclaim. The 4-hour turn is
    added by ``confirm_filter`` for channel passes only."""
    return {"pivot": daily_pivot(bars, swings, side), CLOSE_RECLAIM: close_reclaim(bars, line, side),
            FOUR_HOUR_TURN: None, "passed_by": None}


def _round(value: float | None) -> float | None:
    return round(float(value), 4) if value is not None and np.isfinite(value) else None
