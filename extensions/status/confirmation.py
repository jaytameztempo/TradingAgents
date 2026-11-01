"""Confirmation filters for the paper tickets (the TRADE Bot layer). A channel member is ticketed only on a day its
confirmation fires, so a name can sit in the channel basket for days first. Nothing here reads the network, writes a
file or places an order, and no Alpaca module is loaded: the ticket runner reads the bars
(``paper_orders.session_bars``) and passes them in.

On daily bars dated on or before as_of (the last one must be the as-of session):

- trigger, long: the TTM Squeeze released bullish on the as-of session or the one before, or RSI(14) <= 45;
  short: released bearish on one of those two sessions, or RSI(14) >= 55. The squeeze uses the ticket gate's rules
  (``squeeze_gate``: 20 bars, Bollinger 2.0, Keltner 1.5) on daily bars: the release is the first "off" bar after an
  "on" bar with no "on" bar since, its direction the momentum's sign on that bar, and the latest bar's momentum must
  still be on the trade's side of zero. The 4-hour and 1-hour squeeze gate is not used here,
- volume light: the as-of session's volume is at most its 20-session average (the 20 sessions ending on as_of).

Both must hold. A name that fails is skipped with the reason ("awaiting squeeze release or RSI extreme", "volume not
light").

Recorded, never filtered on: whether the last regular-session 4-hour bar closed within 0.5 ATR(14) (4-hour) of the
name's own 4-hour line, support for a long and resistance for a short, drawn by the daily channel rule
(``swings.channel_lines``: the last two confirmed 5% ZigZag swings) on the last 60 4-hour bars.

Sizing: when ADX(14) on the as-of daily bar is under 15 for both SPY and QQQ, the basket allocation is multiplied by
0.7 before it is split over the names (``calm_scale``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from extensions.regime.indicators import sma, wilder_adx
from extensions.scanbot.swings import RSI_PERIOD, channel_lines, wilder_atr, wilder_rsi, zigzag_swings
from extensions.status import squeeze_gate
from extensions.status.daily_status import LONG, SHORT

RSI_LONG_MAX, RSI_SHORT_MIN = 45.0, 55.0
VOLUME_WINDOW = 20
RELEASE_SESSIONS = 2  # the as-of session or the one before
FOUR_HOUR_NEAR_ATR = 0.5
FOUR_HOUR_WINDOW = 60  # 4-hour bars the 4-hour line is drawn on, as the daily channel uses 60 sessions
ATR_PERIOD = ADX_PERIOD = 14
ADX_CALM_MAX = 15.0  # both index readings under this scale the basket down
CALM_SCALE = 0.7
INDEXES = ("SPY", "QQQ")
DAILY_LOOKBACK_DAYS = 400  # calendar days of daily bars, as the scans read (regime_bot.LOOKBACK_CALENDAR_DAYS)
FOUR_HOUR_LOOKBACK_DAYS = squeeze_gate.BARS_LOOKBACK_DAYS

AWAITING_TRIGGER = "awaiting squeeze release or RSI extreme"
VOLUME_NOT_LIGHT = "volume not light"
NO_DAILY_BAR = "no daily bar on the as-of date"
SHORT_HISTORY = "too few daily bars"

DIRECTION = {LONG: "bullish", SHORT: "bearish"}
FOUR_HOUR_LINE = {LONG: "support", SHORT: "resistance"}


@dataclass(frozen=True)
class MarketData:
    """Bars the ticket runner read for one ticket run: daily (symbol, date, open, high, low, close, volume) for the
    names and the indexes, and regular-session 4-hour bars (``squeeze_gate.roll_session_bars``) for the names."""
    daily: pd.DataFrame
    four_hour: pd.DataFrame
    source: str


@dataclass(frozen=True)
class Confirmation:
    symbol: str
    side: str
    passed: bool
    reasons: list[str]  # empty when it passes
    triggers: list[str]  # squeeze_release and/or rsi, when either fired
    squeeze: dict
    rsi_14: float | None
    volume: float | None
    volume_avg_20: float | None
    last_bar_date: str | None
    four_hour: dict = field(default_factory=dict)  # recorded only

    def to_dict(self) -> dict:
        return asdict(self)


def _num(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, 4) if np.isfinite(value) else None


def _daily_for(daily: pd.DataFrame, symbol: str, as_of: date) -> pd.DataFrame:
    if daily is None or daily.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    rows = daily[(daily["symbol"] == symbol) & (daily["date"] <= pd.Timestamp(as_of))]
    return rows.dropna(subset=["high", "low", "close"]).sort_values("date").reset_index(drop=True)


def daily_squeeze(bars: pd.DataFrame, side: str) -> dict:
    """The daily TTM Squeeze: whether it released in the trade's direction on the last RELEASE_SESSIONS bars and the
    last bar's momentum still agrees. bars are one name's daily bars, oldest first, ending on as_of."""
    out = {"released": False, "fire_date": None, "fire_momentum": None, "last_state": None, "last_momentum": None,
           "detail": ""}
    if len(bars) < squeeze_gate.MIN_BARS:
        out["detail"] = f"{len(bars)} daily bars, {squeeze_gate.MIN_BARS} needed for the squeeze"
        return out
    sq = squeeze_gate.ttm_squeeze(bars.assign(timestamp=bars["date"]))
    last = sq.iloc[-1]
    out.update(last_state=last["state"], last_momentum=_num(last["momentum"]))
    at = squeeze_gate.last_fire(sq)
    if at is None:
        out["detail"] = f"daily squeeze has not released (squeeze {last['state']} on {last['date']:%Y-%m-%d})"
        return out
    fire = sq.iloc[at]
    out.update(fire_date=fire["date"].date().isoformat(), fire_momentum=_num(fire["momentum"]))
    if at < len(sq) - RELEASE_SESSIONS:
        out["detail"] = f"daily squeeze last released {fire['date']:%Y-%m-%d}, before the last {RELEASE_SESSIONS} sessions"
        return out
    sign = 1 if side == LONG else -1
    fired, now = out["fire_momentum"], out["last_momentum"]
    if fired is None or not sign * fired > 0:
        out["detail"] = f"daily squeeze released {fire['date']:%Y-%m-%d} with momentum {fired}, not {DIRECTION[side]}"
    elif now is None or not sign * now > 0:
        out["detail"] = (f"daily squeeze released {DIRECTION[side]} {fire['date']:%Y-%m-%d}, but the last bar's momentum "
                         f"{now} no longer agrees")
    else:
        out.update(released=True, detail=f"daily squeeze released {DIRECTION[side]} {fire['date']:%Y-%m-%d} "
                                         f"(momentum {fired:+.4f})")
    return out


def four_hour_flag(bars: pd.DataFrame, side: str, as_of: date) -> dict:
    """Recorded only: whether the last 4-hour bar on or before as_of closed within FOUR_HOUR_NEAR_ATR x its 4-hour
    ATR(14) of its own 4-hour line (support for a long, resistance for a short). near is None when it cannot be
    measured."""
    line_word = FOUR_HOUR_LINE[side]
    out = {"near": None, "line": line_word, "level": None, "close": None, "atr_14": None, "distance_atr": None,
           "max_atr": FOUR_HOUR_NEAR_ATR, "bar": None, "swings": [], "detail": ""}
    if bars is not None and not bars.empty:
        bars = bars[bars["date"] <= pd.Timestamp(as_of)].sort_values("timestamp").reset_index(drop=True)
    if bars is None or len(bars) < FOUR_HOUR_WINDOW:
        out["detail"] = f"{0 if bars is None else len(bars)} 4-hour bars, {FOUR_HOUR_WINDOW} needed"
        return out
    high, low, close = bars["high"], bars["low"], bars["close"]
    atr = float(wilder_atr(high, low, close, ATR_PERIOD).iloc[-1])
    first = len(bars) - FOUR_HOUR_WINDOW
    swings = [s for s in zigzag_swings(high, low, close) if s.index >= first]
    support, resistance, low_pair, high_pair = channel_lines(swings)
    level, pair = (support, low_pair) if side == LONG else (resistance, high_pair)
    last_close = float(close.iloc[-1])
    out.update(close=_num(last_close), atr_14=_num(atr), bar=bars["timestamp"].iloc[-1].isoformat(),
               swings=[{"bar": bars["timestamp"].iloc[s.index].isoformat(), "price": _num(s.price)} for s in pair])
    if level is None or not np.isfinite(atr) or atr <= 0:
        out["detail"] = f"no 4-hour {line_word} (no confirmed swing in the last {FOUR_HOUR_WINDOW} 4-hour bars)"
        return out
    distance = (last_close - level) / atr
    near = bool(abs(distance) <= FOUR_HOUR_NEAR_ATR + 1e-9)
    out.update(near=near, level=_num(level), distance_atr=_num(distance),
               detail=f"last 4-hour close {last_close:.2f} is {distance:+.2f} ATR from 4-hour {line_word} {level:.2f}")
    return out


def confirm(symbol: str, side: str, market: MarketData, as_of: date) -> Confirmation:
    """The confirmation filters for one channel member on the as-of session."""
    bars = _daily_for(market.daily, symbol, as_of)
    four = market.four_hour
    four = four[four["symbol"] == symbol] if four is not None and not four.empty else four
    flag = four_hour_flag(four, side, as_of)
    last_date = bars["date"].iloc[-1].date().isoformat() if not bars.empty else None
    if last_date != as_of.isoformat():
        return Confirmation(symbol, side, False, [f"{NO_DAILY_BAR} ({as_of}; last bar {last_date})"], [], {}, None,
                            None, None, last_date, flag)
    if len(bars) < max(squeeze_gate.MIN_BARS, VOLUME_WINDOW, RSI_PERIOD + 1):
        return Confirmation(symbol, side, False, [f"{SHORT_HISTORY} ({len(bars)})"], [], {}, None, None, None,
                            last_date, flag)

    squeeze = daily_squeeze(bars, side)
    rsi = _num(wilder_rsi(bars["close"], RSI_PERIOD).iloc[-1])
    volume = bars["volume"].astype(float)
    last_volume, average = _num(volume.iloc[-1]), _num(sma(volume, VOLUME_WINDOW).iloc[-1])

    triggers = ["squeeze_release"] if squeeze["released"] else []
    rsi_hit = rsi is not None and (rsi <= RSI_LONG_MAX if side == LONG else rsi >= RSI_SHORT_MIN)
    if rsi_hit:
        triggers.append("rsi")
    reasons = []
    if not triggers:
        limit = f"<= {RSI_LONG_MAX:g}" if side == LONG else f">= {RSI_SHORT_MIN:g}"
        reasons.append(f"{AWAITING_TRIGGER} ({squeeze['detail']}; RSI(14) {rsi}, needs {limit})")
    if last_volume is None or average is None or not last_volume <= average:
        reasons.append(f"{VOLUME_NOT_LIGHT} (volume {last_volume:,.0f} over its {VOLUME_WINDOW}-session average "
                       f"{average:,.0f})" if last_volume is not None and average is not None
                       else f"{VOLUME_NOT_LIGHT} (no volume reading)")
    return Confirmation(symbol, side, not reasons, reasons, triggers, squeeze, rsi, last_volume, average, last_date,
                        flag)


def index_adx(daily: pd.DataFrame, as_of: date, symbols: tuple[str, ...] = INDEXES) -> dict[str, float | None]:
    """ADX(14) on each index's as-of daily bar; None when there is no bar on as_of or too few bars."""
    out: dict[str, float | None] = {}
    for symbol in symbols:
        bars = _daily_for(daily, symbol, as_of)
        if bars.empty or bars["date"].iloc[-1].date() != as_of:
            out[symbol] = None
            continue
        out[symbol] = _num(wilder_adx(bars["high"], bars["low"], bars["close"], ADX_PERIOD).iloc[-1])
    return out


def calm_scale(readings: dict[str, float | None]) -> float:
    """CALM_SCALE when every index reading is under ADX_CALM_MAX, else 1. The caller refuses missing readings."""
    return CALM_SCALE if readings and all(v is not None and v < ADX_CALM_MAX for v in readings.values()) else 1.0


def rules() -> dict:
    return {
        "confirmation": "a channel member is ticketed only when its trigger and light volume both hold on the as-of "
                        "daily bar",
        "trigger_long": f"daily TTM Squeeze released bullish on the as-of session or the one before, or RSI(14) <= "
                        f"{RSI_LONG_MAX:g}",
        "trigger_short": f"daily TTM Squeeze released bearish on the as-of session or the one before, or RSI(14) >= "
                         f"{RSI_SHORT_MIN:g}",
        "squeeze": f"daily bars; length {squeeze_gate.LENGTH}, Bollinger {squeeze_gate.BB_MULT:g}, Keltner "
                   f"{squeeze_gate.KC_MULT:g}; the release's momentum and the last bar's must both be on the trade's "
                   "side of zero",
        "volume_light": f"the as-of session's volume is at most its {VOLUME_WINDOW}-session average",
        "four_hour_flag": f"recorded, not filtered: the last 4-hour close within {FOUR_HOUR_NEAR_ATR:g} x 4-hour "
                          f"ATR(14) of the 4-hour support (long) or resistance (short), drawn from the last two "
                          f"confirmed swings in the last {FOUR_HOUR_WINDOW} 4-hour bars",
        "calm_scale_rule": f"when ADX(14) is under {ADX_CALM_MAX:g} for both {' and '.join(INDEXES)} on the as-of daily bar, "
                      f"the basket allocation x {CALM_SCALE:g} before it is split over N",
    }
