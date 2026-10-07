"""SCANBot build step 4: pivot structure on the UP and DOWN names of a saved trend report.

Each name is checked on split-adjusted SIP daily bars dated on or before as_of.
Swings come from a ZigZag with a 5% reversal on highs and lows. A swing is
confirmed only by a later bar whose close is at least 5% off that extreme, so
the reversal has closed. A pending, unconfirmed extreme is never a swing.

UP (bottom pivot, SCANBot.md 4.1), all required:

- the last confirmed swing is a high, and the swing before it is a low
- that impulse, low to high, is 6% to 35%
- close has given back 35% to 50% of the impulse
- close is above SMA50
- the pullback, sessions after the swing high through as_of, is 3 to 15

DOWN (top pivot, 4.2) is the mirror: last confirmed swing is a low after a high,
the decline is 6% to 35%, close has taken back 35% to 50% of it, close is below
SMA50, the bounce is 3 to 15 sessions, and easy_to_borrow is true on the asset
snapshot. Missing structure is a fail, with every reason that applies.

The four quality checks are computed and recorded for every name, but they are
not required here. This step builds no channel scans, ranking or baskets.
Nothing here places an order.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from extensions.market_data.alpaca_bars import MARKET_TZ, fetch_daily_bars
from extensions.regime.indicators import sma
from extensions.regime.regime_bot import FAST_WINDOW, LOOKBACK_CALENDAR_DAYS
from extensions.scanbot.funnel import FEED, _fetch_in_chunks, default_report_dir
from extensions.scanbot.trend import DOWN, UP, TrendReport

REPORT_VERSION = 1
SIDES = (UP, DOWN)
SWING_HIGH, SWING_LOW = "HIGH", "LOW"

SWING_REVERSAL_PCT = 5.0
IMPULSE_MIN_PCT, IMPULSE_MAX_PCT = 6.0, 35.0
ZONE_MIN_PCT, ZONE_MAX_PCT = 35.0, 50.0
LEG_MIN_SESSIONS, LEG_MAX_SESSIONS = 3, 15
MIN_BARS = FAST_WINDOW

ATR_PERIOD = RSI_PERIOD = 14
SMA20_WINDOW = 20
SMA20_SLOPE_SESSIONS = 5
SMA20_ATR_MAX = 0.75
RSI_BANDS = {UP: (40.0, 52.0), DOWN: (48.0, 60.0)}
VOLUME_FAST, VOLUME_SLOW = 5, 20
DRYUP_VOLUME_RATIO = 0.80
CLOSE_LOCATION_SESSIONS = 2
QUARTILE = 0.25
MIN_QUALITY_CHECKS = 2  # recorded only; not a gate in this step

NO_BARS = "no_bars"
SHORT_HISTORY = "short_history"
NO_IMPULSE = "no_confirmed_impulse"
IMPULSE_TOO_SMALL = "impulse_under_6pct"
IMPULSE_TOO_LARGE = "impulse_over_35pct"
ZONE_TOO_SHALLOW = "retrace_under_35pct"
ZONE_TOO_DEEP = "retrace_over_50pct"
WRONG_SIDE_SMA50 = "wrong_side_of_sma50"
LEG_TOO_SHORT = "leg_under_3_sessions"
LEG_TOO_LONG = "leg_over_15_sessions"
NOT_EASY_TO_BORROW = "not_easy_to_borrow"
REASONS = (
    NO_BARS, SHORT_HISTORY, NO_IMPULSE, IMPULSE_TOO_SMALL, IMPULSE_TOO_LARGE, ZONE_TOO_SHALLOW,
    ZONE_TOO_DEEP, WRONG_SIDE_SMA50, LEG_TOO_SHORT, LEG_TOO_LONG, NOT_EASY_TO_BORROW,
)

Q_SMA20 = "near_sloped_sma20"
Q_RSI = "rsi_in_band"
Q_VOLUME = "volume_dryup"
Q_CLOSE_LOCATION = "closes_off_extreme_quartile"
QUALITY_CHECKS = (Q_SMA20, Q_RSI, Q_VOLUME, Q_CLOSE_LOCATION)

POLICY = {
    "feed": FEED,
    "adjustment": "split",
    "swing_method": "zigzag_high_low_confirmed_on_close",
    "swing_reversal_pct": SWING_REVERSAL_PCT,
    "impulse_min_pct": IMPULSE_MIN_PCT,
    "impulse_max_pct": IMPULSE_MAX_PCT,
    "retrace_zone_pct": [ZONE_MIN_PCT, ZONE_MAX_PCT],
    "sma50_window": FAST_WINDOW,
    "leg_sessions": [LEG_MIN_SESSIONS, LEG_MAX_SESSIONS],
    "down_requires_easy_to_borrow": True,
    "quality_required": False,
    "quality_min_checks_recorded": MIN_QUALITY_CHECKS,
    "atr_period": ATR_PERIOD,
    "rsi_period": RSI_PERIOD,
    "sma20_slope_sessions": SMA20_SLOPE_SESSIONS,
    "sma20_atr_max": SMA20_ATR_MAX,
    "rsi_bands": {side: list(band) for side, band in RSI_BANDS.items()},
    "volume_sma": [VOLUME_FAST, VOLUME_SLOW],
    "dryup_volume_ratio": DRYUP_VOLUME_RATIO,
    "close_location_sessions": CLOSE_LOCATION_SESSIONS,
    "lookback_calendar_days": LOOKBACK_CALENDAR_DAYS,
}


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


# --- one name ----------------------------------------------------------------

@dataclass(frozen=True)
class PivotCall:
    side: str
    structure_pass: bool
    reasons: list[str]  # codes from REASONS; empty when the structure passes
    detail: list[str]
    quality: dict[str, bool | None]  # None when the inputs do not exist yet
    quality_passed: int
    metrics: dict


def check_structure(
    side: str, *, impulse_pct: float, retrace_pct: float, close: float, sma50: float,
    leg_sessions: int, easy_to_borrow: bool | None = None,
) -> tuple[list[str], list[str]]:
    """Apply the structure rules to the measured numbers. Returns (reason codes, detail); no codes is a pass."""
    leg = "pullback" if side == UP else "bounce"
    codes, detail = [], []
    if impulse_pct < IMPULSE_MIN_PCT:
        codes.append(IMPULSE_TOO_SMALL)
        detail.append(f"impulse {impulse_pct:.2f}% is under {IMPULSE_MIN_PCT:g}%")
    if impulse_pct > IMPULSE_MAX_PCT:
        codes.append(IMPULSE_TOO_LARGE)
        detail.append(f"impulse {impulse_pct:.2f}% is over {IMPULSE_MAX_PCT:g}%")
    if retrace_pct < ZONE_MIN_PCT:
        codes.append(ZONE_TOO_SHALLOW)
        detail.append(f"{leg} retraced {retrace_pct:.1f}% of the impulse, under {ZONE_MIN_PCT:g}%")
    if retrace_pct > ZONE_MAX_PCT:
        codes.append(ZONE_TOO_DEEP)
        detail.append(f"{leg} retraced {retrace_pct:.1f}% of the impulse, over {ZONE_MAX_PCT:g}%")
    if not (close > sma50 if side == UP else close < sma50):
        codes.append(WRONG_SIDE_SMA50)
        detail.append(f"close {close:.2f} is not {'above' if side == UP else 'below'} SMA{FAST_WINDOW} {sma50:.2f}")
    if leg_sessions < LEG_MIN_SESSIONS:
        codes.append(LEG_TOO_SHORT)
        detail.append(f"{leg} is {leg_sessions} sessions, under {LEG_MIN_SESSIONS}")
    if leg_sessions > LEG_MAX_SESSIONS:
        codes.append(LEG_TOO_LONG)
        detail.append(f"{leg} is {leg_sessions} sessions, over {LEG_MAX_SESSIONS}")
    if side == DOWN and easy_to_borrow is not True:
        codes.append(NOT_EASY_TO_BORROW)
        detail.append(_borrow_text(easy_to_borrow))
    return codes, detail


def _borrow_text(easy_to_borrow: bool | None) -> str:
    state = "missing from the asset snapshot" if easy_to_borrow is None else "false on the asset snapshot"
    return f"easy_to_borrow is {state}"


def quality_checks(side: str, bars: pd.DataFrame) -> tuple[dict[str, bool | None], dict]:
    """The four quality checks on the last bar. Recorded, not required."""
    high, low, close, volume = bars["high"], bars["low"], bars["close"], bars["volume"].astype(float)
    sma20 = sma(close, SMA20_WINDOW)
    atr = float(wilder_atr(high, low, close).iloc[-1])
    rsi = float(wilder_rsi(close).iloc[-1])
    vol_fast, vol_slow = float(sma(volume, VOLUME_FAST).iloc[-1]), float(sma(volume, VOLUME_SLOW).iloc[-1])
    now20 = float(sma20.iloc[-1])
    then20 = float(sma20.iloc[-1 - SMA20_SLOPE_SESSIONS]) if len(sma20) > SMA20_SLOPE_SESSIONS else np.nan

    def known(*values):
        return all(np.isfinite(v) for v in values)

    distance_atr = abs(float(close.iloc[-1]) - now20) / atr if known(now20, atr) and atr > 0 else np.nan
    slope_ok = (now20 > then20 if side == UP else now20 < then20) if known(now20, then20) else None
    near = bool(distance_atr <= SMA20_ATR_MAX) if np.isfinite(distance_atr) else None
    rsi_low, rsi_high = RSI_BANDS[side]

    recent = bars.tail(CLOSE_LOCATION_SESSIONS)
    span = (recent["high"] - recent["low"]).to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        location = np.where(span > 0, (recent["close"] - recent["low"]).to_numpy(dtype=float) / span, 0.5)
    off_extreme = bool(np.all(location > QUARTILE) if side == UP else np.all(location < 1 - QUARTILE))

    quality = {
        Q_SMA20: None if slope_ok is None or near is None else bool(slope_ok and near),
        Q_RSI: bool(rsi_low <= rsi <= rsi_high) if np.isfinite(rsi) else None,
        Q_VOLUME: bool(vol_fast < DRYUP_VOLUME_RATIO * vol_slow) if known(vol_fast, vol_slow) else None,
        Q_CLOSE_LOCATION: off_extreme if len(recent) == CLOSE_LOCATION_SESSIONS else None,
    }
    metrics = {
        "sma_20": _round(now20),
        "sma_20_sloped_right_way": slope_ok,
        "distance_to_sma20_atr": _round(distance_atr),
        f"atr_{ATR_PERIOD}": _round(atr),
        f"rsi_{RSI_PERIOD}": _round(rsi),
        "volume_ratio_5_20": _round(vol_fast / vol_slow) if known(vol_fast, vol_slow) and vol_slow > 0 else None,
        "close_location_last_2": [_round(float(x)) for x in location],
    }
    return quality, metrics


def classify_pivot(side: str, bars: pd.DataFrame, easy_to_borrow: bool | None = None) -> PivotCall:
    """Check one symbol's bars. The caller has already dropped bars after as_of."""
    if side not in SIDES:
        raise ValueError(f"side must be {UP} or {DOWN}, not {side!r}")
    bars = bars.dropna(subset=["high", "low", "close"]).sort_values("date").reset_index(drop=True)
    count = len(bars)
    # Missing structure stops the measuring, but the borrow check still applies to a DOWN name.
    borrow_codes, borrow_detail = ([NOT_EASY_TO_BORROW], [_borrow_text(easy_to_borrow)]) if (
        side == DOWN and easy_to_borrow is not True) else ([], [])
    if count == 0:
        return PivotCall(side, False, [NO_BARS, *borrow_codes], ["no daily bars on or before as_of", *borrow_detail],
                         {}, 0, {"bars": 0})
    last_bar = bars["date"].iloc[-1].date().isoformat()
    if count < MIN_BARS:
        return PivotCall(side, False, [SHORT_HISTORY, *borrow_codes],
                         [f"only {count} daily bars; needs {MIN_BARS}", *borrow_detail],
                         {}, 0, {"bars": count, "last_bar_date": last_bar})

    quality, quality_metrics = quality_checks(side, bars)
    quality_passed = sum(1 for v in quality.values() if v)
    close = float(bars["close"].iloc[-1])
    sma50 = float(sma(bars["close"], FAST_WINDOW).iloc[-1])
    metrics = {"close": _round(close), f"sma_{FAST_WINDOW}": _round(sma50), "bars": count, "last_bar_date": last_bar}
    if side == DOWN:
        metrics["easy_to_borrow"] = easy_to_borrow

    swings = zigzag_swings(bars["high"], bars["low"], bars["close"])
    end_kind, start_kind = (SWING_HIGH, SWING_LOW) if side == UP else (SWING_LOW, SWING_HIGH)
    if len(swings) < 2 or swings[-1].kind != end_kind:
        last = swings[-1].kind.lower() if swings else "none"
        detail = [
            f"no confirmed {start_kind.lower()}-to-{end_kind.lower()} impulse as the last swing (last confirmed swing: {last})",
            *borrow_detail,
        ]
        metrics["last_swing"] = _swing_dict(swings[-1], bars) if swings else None
        return PivotCall(side, False, [NO_IMPULSE, *borrow_codes], detail, quality, quality_passed,
                         {**metrics, **quality_metrics})

    start, end = swings[-2], swings[-1]
    assert start.kind == start_kind  # ZigZag swings alternate
    move = abs(end.price - start.price)
    impulse_pct = 100 * move / start.price
    retrace_pct = 100 * ((end.price - close) if side == UP else (close - end.price)) / move
    leg_sessions = count - 1 - end.index
    codes, detail = check_structure(
        side, impulse_pct=impulse_pct, retrace_pct=retrace_pct, close=close, sma50=sma50,
        leg_sessions=leg_sessions, easy_to_borrow=easy_to_borrow,
    )
    if not codes:
        leg = "pullback" if side == UP else "bounce"
        detail = [
            f"impulse {impulse_pct:.2f}% from {start.price:.2f} to {end.price:.2f}",
            f"{leg} retraced {retrace_pct:.1f}% over {leg_sessions} sessions",
            f"close {close:.2f} {'above' if side == UP else 'below'} SMA{FAST_WINDOW} {sma50:.2f}",
        ]
    metrics.update({
        "impulse_start": _swing_dict(start, bars),
        "impulse_end": _swing_dict(end, bars),
        "impulse_pct": _round(impulse_pct),
        "retrace_pct": _round(retrace_pct),
        "leg_sessions": leg_sessions,
    })
    return PivotCall(side, not codes, codes, detail, quality, quality_passed, {**metrics, **quality_metrics})


def _swing_dict(swing: Swing, bars: pd.DataFrame) -> dict:
    return {
        "kind": swing.kind,
        "date": bars["date"].iloc[swing.index].date().isoformat(),
        "price": _round(swing.price),
        "confirmed_on": bars["date"].iloc[swing.confirmed_index].date().isoformat(),
    }


def _round(value: float) -> float | None:
    return round(float(value), 4) if value is not None and np.isfinite(value) else None


# --- the run -----------------------------------------------------------------

@dataclass(frozen=True)
class PivotReport:
    as_of: date
    created_at: datetime
    trend_report: str
    trend_created_at: str
    assets_source: str
    assets_fetched_at: str
    policy: dict
    counts: dict[str, dict]  # per side: entered, pass/fail, reason and quality counts
    passed: dict[str, list[str]]  # structure passes per side; quality not required
    calls: dict[str, dict[str, dict]]  # side -> symbol -> PivotCall
    warnings: list[str] = field(default_factory=list)
    report_version: int = REPORT_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> PivotReport:
        if data.get("report_version") != REPORT_VERSION:
            raise ValueError(f"unsupported pivot report_version: {data.get('report_version')!r}")
        fields = dict(data)
        fields["as_of"] = date.fromisoformat(data["as_of"])
        fields["created_at"] = datetime.fromisoformat(data["created_at"])
        return cls(**fields)


def _side_counts(calls: dict[str, PivotCall]) -> dict:
    passing = [c for c in calls.values() if c.structure_pass]
    return {
        "entered": len(calls),
        "structure_pass": len(passing),
        "structure_fail": len(calls) - len(passing),
        "reason_counts": {code: sum(1 for c in calls.values() if code in c.reasons) for code in REASONS},
        "quality_counts_all": {q: sum(1 for c in calls.values() if c.quality.get(q)) for q in QUALITY_CHECKS},
        "quality_counts_pass": {q: sum(1 for c in passing if c.quality.get(q)) for q in QUALITY_CHECKS},
        "pass_quality_histogram": {str(k): sum(1 for c in passing if c.quality_passed == k) for k in range(5)},
        f"pass_with_{MIN_QUALITY_CHECKS}_quality": sum(1 for c in passing if c.quality_passed >= MIN_QUALITY_CHECKS),
    }


def run_pivot(
    trend: TrendReport,
    trend_path: str | Path,
    borrow: dict[str, bool],
    assets_source: str,
    assets_fetched_at: datetime,
    *,
    bars_fetcher: Callable = fetch_daily_bars,
    cache_dir: str | Path | None = None,
    created_at: datetime | None = None,
) -> PivotReport:
    """Check every UP and DOWN name of the trend report. ``borrow`` maps symbol to easy_to_borrow. A bars failure raises."""
    as_of = trend.as_of
    names = {UP: list(trend.up), DOWN: list(trend.down)}
    symbols = names[UP] + names[DOWN]
    start = as_of - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    bars = _fetch_in_chunks(bars_fetcher, symbols, start, as_of, cache_dir) if symbols else pd.DataFrame()
    if not bars.empty:
        bars = bars[bars["date"] <= pd.Timestamp(as_of)]
    by_symbol = dict(tuple(bars.groupby("symbol"))) if not bars.empty else {}
    empty = pd.DataFrame({"date": pd.to_datetime([]), "high": [], "low": [], "close": [], "volume": []})

    calls = {
        side: {s: classify_pivot(side, by_symbol.get(s, empty), borrow.get(s) if side == DOWN else None)
               for s in sorted(names[side])}
        for side in SIDES
    }
    counts = {side: _side_counts(calls[side]) for side in SIDES}
    for side in SIDES:
        c = counts[side]
        if c["structure_pass"] + c["structure_fail"] != len(names[side]) or c["entered"] != len(names[side]):
            raise AssertionError(f"pivot lost track of {side} names: {len(names[side])} in, {c}")

    warnings = [w for w in trend.warnings if "point in time" in w]
    if assets_fetched_at.astimezone(MARKET_TZ).date() > as_of:
        warnings.append(
            f"easy_to_borrow comes from an asset snapshot fetched {assets_fetched_at.isoformat()}, "
            "after as_of: it is not point in time"
        )
    last_dates = {
        s: c.metrics.get("last_bar_date") for side in SIDES for s, c in calls[side].items() if c.metrics.get("last_bar_date")
    }
    if last_dates:
        newest = max(last_dates.values())
        stale = sorted(s for s, d in last_dates.items() if d < newest)
        if stale:
            warnings.append(f"{len(stale)} names have no bar on {newest}: {', '.join(stale)}")

    return PivotReport(
        as_of=as_of,
        created_at=created_at or datetime.now(UTC),
        trend_report=str(trend_path),
        trend_created_at=trend.created_at.isoformat(),
        assets_source=str(assets_source),
        assets_fetched_at=assets_fetched_at.isoformat(),
        policy=dict(POLICY),
        counts=counts,
        passed={side: sorted(s for s, c in calls[side].items() if c.structure_pass) for side in SIDES},
        calls={side: {s: asdict(c) for s, c in calls[side].items()} for side in SIDES},
        warnings=warnings,
    )


def borrow_flags(assets: list[dict]) -> dict[str, bool]:
    """symbol -> easy_to_borrow from Alpaca asset records."""
    return {a["symbol"]: bool(a.get("easy_to_borrow")) for a in assets if a.get("symbol")}


def save_report(report: PivotReport, out_dir: str | Path | None = None) -> Path:
    """Write the report as a new file and return its path. An existing report is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_report_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"pivot_{report.as_of}_{report.created_at:%Y%m%dT%H%M%SZ}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2)
        fh.write("\n")
    return path


def load_report(path: str | Path) -> PivotReport:
    return PivotReport.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
