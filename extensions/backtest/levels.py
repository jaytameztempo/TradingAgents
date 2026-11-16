"""Stops and targets for the names a run date would have traded.

SIDE (recorded): the levels the live paper tickets use, through ``paper_tickets.plan_ticket``. The line (support for
a long, resistance for a short), atr_14 and midline come from the side handoff's ``levels`` (or, with no handoff, from
the channel report, midline = (support + resistance) / 2), and the clustered stop swing from the channel report.
Stop = stop swing -/+ STOP_ATR (1.0) x atr_14, snapped to .99/.49 (long) or .01/.51 (short). Target = the midline.
Channel reports written before the stop-swing rule record no stop swing; for a handoff name the handoff's recorded
``invalidation`` (0.25 ATR through the line) is then the stop.

UP / DOWN (derived; the trend baskets record no stop or target):
- stop: the last confirmed swing low - STOP_ATR (1.0) x ATR(14), snapped down with ``snap_stop`` (long, UPBot);
  the last confirmed swing high + 1.0 x ATR(14), snapped up (short, DOWNBot). The swing and ATR are the basket's
  recorded ``last_swing_low`` / ``last_swing_high`` and ``atr_14`` when it has them, otherwise they are measured on
  cached daily bars ending on the report's as-of (5% ZigZag, confirmed swings only; Wilder ATR 14), as the scans do.
- target: entry +/- TARGET_R (2) x the risk, where the entry is the fill and the risk is |entry - stop|.
- a name whose risk at the fill is over MAX_RISK_ATR (3) x ATR is skipped: the swing is too far to be a stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from extensions.scanbot.swings import SWING_HIGH, SWING_LOW, wilder_atr, zigzag_swings
from extensions.status.paper_tickets import STOP_ATR, plan_ticket, snap_stop

LONG, SHORT = "long", "short"
TARGET_R = 2.0
MAX_RISK_ATR = 3.0
ATR_PERIOD = 14
STOP_SWING = {LONG: "stop_swing_low", SHORT: "stop_swing_high"}
TREND_SWING = {LONG: "last_swing_low", SHORT: "last_swing_high"}

RULES = {
    "side": "SIDEBot (recorded levels): stop = the channel report's clustered stop swing -/+ 1.0 x ATR14, snapped to "
            ".99/.49 (long) or .01/.51 (short); when the channel report predates the stop swing, the side "
            "handoff's recorded invalidation (0.25 x ATR14 through the line) is the stop; target = the channel "
            "midline; the recorded entry is the limit at support (long) or resistance (short), shown for reference.",
    "trend": "UPBot / DOWNBot (derived levels): stop = last confirmed swing low - 1.0 x ATR14, snapped to .99/.49 "
             "(long), or last confirmed swing high + 1.0 x ATR14, snapped to .01/.51 (short); target = entry +/- 2R; "
             "skipped when the risk at the fill is over 3 x ATR14. Swing and ATR from the basket when recorded, else "
             "from cached daily bars ending on the report's as-of.",
    "fill": "Entry at the open of the first session after the run date (first 4-hour bar on 4-hour bars). Skipped if "
            "that open is already past the stop or the target.",
    "exit": "Stop or target, whichever the bars reach first; both in one bar counts as the stop. A gap past a level "
            "fills at the open. Otherwise the close of the last allowed session (max sessions held); with hold until "
            "stop or target there is no time exit, and a trade that reaches neither before the bars end is open "
            "at the end. No partial fills, slippage or fees.",
    "point_in_time": "A file counts on a run date only if it was written (New York date) on or before it; the newest "
                     "visible file of each kind is used, and a report is acted on once.",
}


class LevelsError(ValueError):
    """No usable stop or target for a name."""


@dataclass(frozen=True)
class Plan:
    """What the simulator needs for one name. ``target`` is None for a trend plan: it is set from the fill."""

    bot: str  # up, down or side
    symbol: str
    side: str  # long or short
    stop: float
    target: float | None
    levels_source: str  # recorded or derived
    reference_entry: float | None = None  # the recorded limit entry (SIDE), for display
    atr: float | None = None
    target_r: float | None = None
    max_risk: float | None = None
    detail: str = ""


def _price(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def side_plan(symbol: str, side: str, line, atr, midline, swing, *, source: str) -> Plan:
    try:
        t = plan_ticket(symbol, side, line, atr, midline, swing)
    except ValueError as exc:
        raise LevelsError(str(exc)) from None
    return Plan(bot="side", symbol=symbol, side=side, stop=t["stop"], target=t["target"], levels_source="recorded",
                reference_entry=t["entry"], atr=atr,
                detail=f"{source}: {t['line']} {t['channel_level']:g}, stop swing {t['stop_swing']:g} "
                       f"({t['stop_swing_date']}), midline {t['midline']:g}")


def side_plan_from_handoff(symbol: str, side: str, entry: dict, channel_report: dict | None) -> Plan:
    """A SIDE plan from a handoff name and the channel report the handoff names."""
    levels = entry.get("levels") or {}
    line = levels.get("support") if side == LONG else levels.get("resistance")
    mid, stop = levels.get("midline"), levels.get("invalidation")
    swing = _channel_metrics(channel_report, symbol).get(STOP_SWING[side])
    try:
        return side_plan(symbol, side, line, levels.get("atr_14"), mid, swing, source="handoff")
    except LevelsError as first:
        # Channel reports written before the stop-swing rule record no stop swing; the handoff's recorded
        # invalidation (0.25 ATR through the line) is then the stop.
        sign = 1 if side == LONG else -1
        if not (_price(line) and _price(mid) and _price(stop)) or not sign * (line - stop) > 0 \
                or not sign * (mid - line) > 0:
            raise first from None
        word = "support" if side == LONG else "resistance"
        return Plan(bot="side", symbol=symbol, side=side, stop=round(stop, 2), target=round(mid, 2),
                    levels_source="recorded", reference_entry=round(line, 2), atr=levels.get("atr_14"),
                    detail=f"handoff: {word} {line:g}, no stop swing in the channel report so stop = the handoff's "
                           f"invalidation {stop:g}, midline {mid:g}")


def side_plan_from_channel(symbol: str, side: str, channel_report: dict) -> Plan:
    """A SIDE plan from a channel report member alone (no handoff): midline = (support + resistance) / 2."""
    m = _channel_metrics(channel_report, symbol)
    support, resistance = m.get("support"), m.get("resistance")
    midline = round((support + resistance) / 2, 4) if _price(support) and _price(resistance) else None
    line = support if side == LONG else resistance
    return side_plan(symbol, side, line, m.get("atr_14"), midline, m.get(STOP_SWING[side]), source="channel report")


def _channel_metrics(report: dict | None, symbol: str) -> dict:
    call = ((report or {}).get("calls") or {}).get(symbol) or {}
    return call.get("metrics") or {}


def swing_from_bars(daily: pd.DataFrame, as_of: date, side: str) -> tuple[float, float, str] | None:
    """(swing price, ATR14, swing date) on daily bars ending on ``as_of``: the last confirmed swing low (long) or
    high (short). None when there are too few bars or no confirmed swing of that kind."""
    upto = daily[daily["session"] <= as_of].reset_index(drop=True)
    if len(upto) <= ATR_PERIOD:
        return None
    swings = zigzag_swings(upto["high"], upto["low"], upto["close"])
    kind = SWING_LOW if side == LONG else SWING_HIGH
    mine = [s for s in swings if s.kind == kind]
    atr = float(wilder_atr(upto["high"], upto["low"], upto["close"], ATR_PERIOD).iloc[-1])
    if not mine or not atr > 0:
        return None
    last = mine[-1]
    return last.price, atr, str(upto["session"].iloc[last.index])


def trend_plan(bot: str, symbol: str, side: str, swing: float, atr: float, how: str) -> Plan:
    if not _price(swing) or not _price(atr):
        raise LevelsError(f"no usable swing or ATR ({swing!r}, {atr!r})")
    sign = 1 if side == LONG else -1
    calculated = round(swing - sign * STOP_ATR * atr, 4)
    stop = snap_stop(side, calculated)
    if not stop > 0:
        raise LevelsError(f"stop {stop} from swing {swing:g} and ATR {atr:g} is not above zero")
    word = "low" if side == LONG else "high"
    return Plan(bot=bot, symbol=symbol, side=side, stop=stop, target=None, levels_source="derived", atr=atr,
                target_r=TARGET_R, max_risk=MAX_RISK_ATR * atr,
                detail=f"swing {word} {swing:g} {'-' if side == LONG else '+'} {STOP_ATR:g} x ATR {atr:.4f} "
                       f"= {calculated:g} ({how}); target {TARGET_R:g}R")


def trend_plan_for(bot: str, symbol: str, side: str, metrics: dict | None, daily: pd.DataFrame,
                   as_of: date) -> Plan:
    """A trend plan from the basket's recorded swing and ATR, else from cached daily bars ending on ``as_of``."""
    metrics = metrics or {}
    swing, atr = metrics.get(TREND_SWING[side]), metrics.get("atr_14")
    if _price(swing) and _price(atr):
        return trend_plan(bot, symbol, side, swing, atr, "recorded in the basket")
    measured = swing_from_bars(daily, as_of, side)
    if measured is None:
        raise LevelsError(f"no recorded swing/ATR and no confirmed swing on cached bars through {as_of}")
    price, atr, when = measured
    return trend_plan(bot, symbol, side, price, atr, f"measured on bars through {as_of}, swing on {when}")
