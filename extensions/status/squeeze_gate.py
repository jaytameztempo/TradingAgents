"""TTM Squeeze confirmation for paper tickets and the channel scans (``extensions.scanbot.long_channel`` and
``short_channel`` run the same gate on their channel passes). A name passes only when the squeeze has fired in the
trade direction on both regular-session 4-hour and 1-hour bars dated on or before the as-of date, and the momentum
on the latest bar of each still agrees: a bullish release on both for a long, a bearish release on both for a short.
One timeframe is not enough.

On each timeframe, over ``LENGTH`` (20) bars:

- Bollinger Bands: SMA(close) +/- ``BB_MULT`` (2) x the population standard deviation of close,
- Keltner Channel: the same SMA +/- ``KC_MULT`` (1.5) x the simple average of true range,
- squeeze on: both bands inside the channel; off: both bands outside it; anything else is neither,
- fired: the first "off" bar after an "on" bar ("neither" bars in between are passed over). A later "on" bar is a
  new squeeze, which has not fired,
- momentum: the end value of the least-squares line through close - ((highest high + lowest low) / 2 + SMA) / 2
  over the last LENGTH bars; above zero is bullish, below zero bearish,
- direction: the momentum's sign on the fire bar,
- still agreeing: the momentum on the latest bar on or before as_of must still be on the trade's side of zero.
  A bullish fire whose latest bar's momentum is now below zero fails (zero fails too).

The fire must be recent: on one of the last ``FIRE_LOOKBACK_SESSIONS`` (5) sessions of that timeframe's bars, on
or before as_of, and the squeeze must not have gone back on since.

The caller passes 30-minute bars (``paper_orders.session_bars`` reads them; nothing here imports it, so the daily
command can load this module). ``roll_session_bars`` drops every bar starting before 09:30 or at or after 16:00
New York, so no pre-market or after-hours trade is in any bar, and rolls the rest into session-anchored bars:

- 4-hour: 09:30-13:30 and 13:30-16:00, as ``alpaca_bars.regular_session_4hour`` builds them,
- 1-hour: 09:30, 10:30, ... 14:30, and 15:30-16:00.

Early-close sessions are not detected. Nothing here reads the network, writes a file or places an order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import numpy as np
import pandas as pd

from extensions.status.daily_status import LONG, SHORT

LENGTH = 20
BB_MULT = 2.0
KC_MULT = 1.5
FIRE_LOOKBACK_SESSIONS = 5  # the ticket gate and the channel scans share this window
TIMEFRAMES = ("4h", "1h")
TIMEFRAME_WORD = {"4h": "4-hour", "1h": "1-hour"}
DIRECTION = {LONG: "bullish", SHORT: "bearish"}
BARS_FEED = "iex"  # paper_orders.session_bars reads this feed for tickets; the scans read the same
BARS_LOOKBACK_DAYS = 120  # calendar days of 30-minute bars read before as_of: ~80 sessions, 160 4-hour bars
MIN_BARS = 2 * LENGTH  # momentum needs 2 x LENGTH - 1 bars, and one more for a state change

ON, OFF, NEITHER = "on", "off", "neither"

SESSION_OPEN_MINUTE, SESSION_CLOSE_MINUTE = 9 * 60 + 30, 16 * 60
SESSION_HOURS = {"4h": 4, "1h": 1}
SESSION_COLUMNS = ["symbol", "timestamp", "date", "open", "high", "low", "close", "volume", "source_bars"]


def roll_session_bars(bars_30min: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Roll 30-minute bars (symbol, New York start timestamp, open, high, low, close, volume) into regular-session
    4h or 1h bars anchored at 09:30. Columns SESSION_COLUMNS; source_bars counts the 30-minute bars in each."""
    span = SESSION_HOURS[timeframe] * 60
    frame = bars_30min.copy()
    frame["minute"] = frame["timestamp"].dt.hour * 60 + frame["timestamp"].dt.minute
    frame = frame[(frame["minute"] >= SESSION_OPEN_MINUTE) & (frame["minute"] < SESSION_CLOSE_MINUTE)].copy()
    if frame.empty:
        return pd.DataFrame(columns=SESSION_COLUMNS).astype({"timestamp": "datetime64[ns]", "date": "datetime64[ns]"})
    frame["date"] = frame["timestamp"].dt.normalize()
    start = SESSION_OPEN_MINUTE + (frame["minute"] - SESSION_OPEN_MINUTE) // span * span
    frame["timestamp"] = frame["date"] + pd.to_timedelta(start, unit="min")
    rolled = (
        frame.sort_values(["symbol", "timestamp"])
        .groupby(["symbol", "timestamp"], sort=True)
        .agg(date=("date", "first"), open=("open", "first"), high=("high", "max"), low=("low", "min"),
             close=("close", "last"), volume=("volume", "sum"), source_bars=("close", "size"))
        .reset_index()
    )
    return rolled.reindex(columns=SESSION_COLUMNS)


def _linreg_end(y: np.ndarray) -> float:
    """The least-squares line through y (against 0..n-1), evaluated at the last point."""
    x = np.arange(len(y), dtype=float)
    xm, ym = x.mean(), y.mean()
    slope = ((x - xm) * (y - ym)).sum() / ((x - xm) ** 2).sum()
    return float(ym + slope * (x[-1] - xm))


def ttm_squeeze(bars: pd.DataFrame) -> pd.DataFrame:
    """The squeeze state (on, off, neither; None before LENGTH bars) and momentum (NaN before 2 x LENGTH - 1 bars)
    of each bar, in timestamp order. Columns: timestamp, date, close, bb_upper, bb_lower, kc_upper, kc_lower, state,
    momentum."""
    frame = bars.sort_values("timestamp").reset_index(drop=True)
    high, low, close = (frame[c].astype(float) for c in ("high", "low", "close"))
    mid = close.rolling(LENGTH, min_periods=LENGTH).mean()
    sd = close.rolling(LENGTH, min_periods=LENGTH).std(ddof=0)
    prev = close.shift(1)
    true_range = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    kc_range = true_range.rolling(LENGTH, min_periods=LENGTH).mean()
    out = pd.DataFrame({"timestamp": frame["timestamp"], "date": frame["date"], "close": close,
                        "bb_upper": mid + BB_MULT * sd, "bb_lower": mid - BB_MULT * sd,
                        "kc_upper": mid + KC_MULT * kc_range, "kc_lower": mid - KC_MULT * kc_range})
    inside = (out["bb_upper"] < out["kc_upper"]) & (out["bb_lower"] > out["kc_lower"])
    outside = (out["bb_upper"] > out["kc_upper"]) & (out["bb_lower"] < out["kc_lower"])
    out["state"] = np.where(inside, ON, np.where(outside, OFF, NEITHER))
    out.loc[mid.isna() | kc_range.isna(), "state"] = None
    donchian = (high.rolling(LENGTH, min_periods=LENGTH).max() + low.rolling(LENGTH, min_periods=LENGTH).min()) / 2
    delta = close - (donchian + mid) / 2
    out["momentum"] = delta.rolling(LENGTH, min_periods=LENGTH).apply(_linreg_end, raw=True)
    return out


def last_fire(squeeze: pd.DataFrame) -> int | None:
    """Row of the latest fire still standing: the first "off" bar after an "on" bar, with no "on" bar since."""
    fire, was_on = None, False
    for i, state in enumerate(squeeze["state"]):
        if state == ON:
            fire, was_on = None, True
        elif state == OFF and was_on:
            fire, was_on = i, False
    return fire


@dataclass(frozen=True)
class SqueezeRead:
    """One timeframe's TTM Squeeze on bars dated on or before as_of."""
    timeframe: str  # 4h or 1h
    bars: int
    last_bar: str | None
    last_state: str | None  # on, off, neither
    last_momentum: float | None
    fired: str | None  # bullish or bearish, when a recent fire stands; else None
    fire_bar: str | None
    fire_momentum: float | None
    detail: str


def read_squeeze(bars: pd.DataFrame, timeframe: str, as_of: date,
                 lookback_sessions: int = FIRE_LOOKBACK_SESSIONS) -> SqueezeRead:
    """The squeeze on one timeframe's bars; bars dated after as_of are dropped first."""
    word = TIMEFRAME_WORD[timeframe]
    bars = bars[bars["date"] <= pd.Timestamp(as_of)] if not bars.empty else bars
    if len(bars) < MIN_BARS:
        return SqueezeRead(timeframe, len(bars), None, None, None, None, None, None,
                           f"{len(bars)} {word} bars on or before {as_of}, {MIN_BARS} needed")
    sq = ttm_squeeze(bars)
    last = sq.iloc[-1]
    base = dict(timeframe=timeframe, bars=len(sq), last_bar=last["timestamp"].isoformat(),
                last_state=last["state"], last_momentum=_num(last["momentum"]))
    at = last_fire(sq)
    if at is None:
        return SqueezeRead(**base, fired=None, fire_bar=None, fire_momentum=None,
                           detail=f"{word} squeeze has not fired (last bar {last['timestamp']:%Y-%m-%d %H:%M}: "
                                  f"squeeze {last['state']})")
    fire = sq.iloc[at]
    sessions = sorted(set(sq["date"]))
    recent = sessions[-lookback_sessions:]
    fire_bar, momentum = fire["timestamp"].isoformat(), _num(fire["momentum"])
    if fire["date"] < recent[0]:
        return SqueezeRead(**base, fired=None, fire_bar=fire_bar, fire_momentum=momentum,
                           detail=f"{word} squeeze last fired {fire['timestamp']:%Y-%m-%d %H:%M}, before the last "
                                  f"{lookback_sessions} sessions")
    if momentum is None or momentum == 0:
        return SqueezeRead(**base, fired=None, fire_bar=fire_bar, fire_momentum=momentum,
                           detail=f"{word} squeeze fired {fire['timestamp']:%Y-%m-%d %H:%M} with no momentum direction")
    direction = "bullish" if momentum > 0 else "bearish"
    return SqueezeRead(**base, fired=direction, fire_bar=fire_bar, fire_momentum=momentum,
                       detail=f"{word} squeeze fired {direction} {fire['timestamp']:%Y-%m-%d %H:%M} "
                              f"(momentum {momentum:+.4f})")


@dataclass(frozen=True)
class SqueezeGate:
    symbol: str
    side: str
    passed: bool
    reason: str | None  # why it failed; None on a pass
    reads: dict[str, SqueezeRead]  # 4h and 1h

    def to_dict(self) -> dict:
        return {tf: asdict(read) for tf, read in self.reads.items()}


def _miss(read: SqueezeRead, want: str) -> str | None:
    """Why one timeframe fails the side's direction, or None: it must have fired that way, and its momentum on the
    latest bar must still be on that side of zero."""
    word = TIMEFRAME_WORD[read.timeframe]
    if read.fired is None:
        return read.detail
    if read.fired != want:
        return f"{word} squeeze fired {read.fired}, not {want}"
    sign = 1 if want == "bullish" else -1
    if read.last_momentum is None or not sign * read.last_momentum > 0:
        now = "flat" if not read.last_momentum else "bearish" if read.last_momentum < 0 else "bullish"
        return (f"{word} squeeze fired {want} but momentum on the latest bar {read.last_bar} is "
                f"{read.last_momentum}, now {now}")
    return None


def gate(symbol: str, side: str, reads: dict[str, SqueezeRead]) -> SqueezeGate:
    """Pass only when both timeframes fired in the side's direction and the latest bar's momentum on both still
    agrees."""
    want = DIRECTION[side]
    misses = [m for m in (_miss(reads[tf], want) for tf in TIMEFRAMES) if m]
    reason = None if not misses else f"TTM Squeeze not {want} on both 4-hour and 1-hour: {'; '.join(misses)}"
    return SqueezeGate(symbol, side, not misses, reason, reads)


def squeeze_gates(sides: dict[str, str], bars_30min: pd.DataFrame, as_of: date,
                  lookback_sessions: int = FIRE_LOOKBACK_SESSIONS) -> dict[str, SqueezeGate]:
    """symbol -> its gate, from 30-minute bars for all symbols (see roll_session_bars)."""
    rolled = {tf: roll_session_bars(bars_30min, tf) for tf in TIMEFRAMES}
    groups = {tf: dict(tuple(frame.groupby("symbol"))) if not frame.empty else {} for tf, frame in rolled.items()}
    empty = rolled["4h"].iloc[0:0]
    return {
        symbol: gate(symbol, side, {tf: read_squeeze(groups[tf].get(symbol, empty), tf, as_of, lookback_sessions)
                                    for tf in TIMEFRAMES})
        for symbol, side in sides.items()
    }


def rules() -> dict:
    return {
        "squeeze_gate": "ticket only when the TTM Squeeze fired bullish (long) or bearish (short) on both the "
                        "regular-session 4-hour and 1-hour bars dated on or before as_of, and the latest bar's "
                        "momentum on both still agrees",
        "squeeze_bars": "30-minute IEX bars, split-adjusted, 09:30-16:00 New York only, rolled into 4h "
                        "(09:30, 13:30) and 1h (09:30 ... 15:30) bars",
        "squeeze_length": LENGTH,
        "squeeze_bollinger": f"SMA({LENGTH}) +/- {BB_MULT:g} x population stdev of close",
        "squeeze_keltner": f"SMA({LENGTH}) +/- {KC_MULT:g} x SMA({LENGTH}) of true range",
        "squeeze_fire": "the first bar with both bands outside the channel after a bar with both inside; a later "
                        "inside bar cancels it",
        "squeeze_fire_lookback_sessions": FIRE_LOOKBACK_SESSIONS,
        "squeeze_momentum": f"linear regression end value over {LENGTH} bars of close - ((highest high + lowest "
                            f"low) / 2 + SMA) / 2; its sign on the fire bar is the direction",
        "squeeze_latest_momentum": "the momentum on the latest 4-hour and the latest 1-hour bar must still agree: "
                                   "above zero (long), below zero (short); zero fails",
    }


def _num(value) -> float | None:
    return None if value is None or pd.isna(value) else round(float(value), 4)
