"""SCAN-LongSidewaysChannel on the STRONG financial survivors of a saved financials report.

Each STRONG survivor is checked on split-adjusted SIP daily bars dated on or
before as_of. Swings come from the pivot step's ZigZag (5% reversal, confirmed
on close, no lookahead). The channel uses the confirmed swings whose extreme
bar is inside the last 60 sessions: resistance is the median of the swing
highs, support the median of the swing lows (SCANBot.md 4.3).

A name passes only if all of these hold:

- it passed the STRONG financial gate (it is only ever drawn from that pass)
- ADX(14) <= 20 on the as-of bar
- channel height, (resistance - support) / close, is 8% to 22%
- at least two support touches and two resistance touches in 60 sessions
- close is inside 0.6 ATR(14) above support, and not through support by
  more than 0.25 ATR

The SMA50 slope, the 50/200 tangle and the four quality checks are computed
and recorded for every measured name, but they are not required here. This
step builds no short channel scan, ranking, score or TickerBasket. Nothing
here places an order.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from extensions.market_data.alpaca_bars import fetch_daily_bars
from extensions.regime.indicators import sma, wilder_adx
from extensions.regime.regime_bot import (
    ADX_PERIOD,
    ADX_SIDE_BELOW,
    FAST_WINDOW,
    LOOKBACK_CALENDAR_DAYS,
    SLOW_WINDOW,
    tangle_check,
)
from extensions.scanbot.financials import STRONG, FinancialsReport
from extensions.scanbot.funnel import FEED, _fetch_in_chunks, default_report_dir
from extensions.scanbot.pivot import (
    ATR_PERIOD,
    RSI_PERIOD,
    SWING_HIGH,
    SWING_LOW,
    SWING_REVERSAL_PCT,
    wilder_atr,
    wilder_rsi,
    zigzag_swings,
)

REPORT_VERSION = 1
SCAN_NAME = "SCAN-LongSidewaysChannel"
MODE = "LONG_SIDE"
PIVOT_SIDE = "LONG"

SIDE_ADX_MAX = ADX_SIDE_BELOW  # 20; the scan allows ADX equal to it
CHANNEL_LOOKBACK = 60
CHANNEL_MIN_HEIGHT_PCT, CHANNEL_MAX_HEIGHT_PCT = 8.0, 22.0
MIN_TOUCHES = 2
PIVOT_ATR_MAX = 0.6
BREAK_ATR_MAX = 0.25
MIN_BARS = CHANNEL_LOOKBACK

# Recorded, not required, in this step.
SLOPE_SESSIONS = 20
SLOPE_MAX_PCT = 0.5
RSI_BAND = (35.0, 48.0)
VOLUME_FAST, VOLUME_SLOW = 5, 20
LOWER_WICK_MIN = 0.40
NO_BREAK_SESSIONS = 3
MIN_QUALITY_CHECKS = 2

NO_BARS = "no_bars"
SHORT_HISTORY = "short_history"
ADX_ABOVE_MAX = "adx_above_20"
TOO_FEW_SUPPORT = "support_touches_under_2"
TOO_FEW_RESISTANCE = "resistance_touches_under_2"
HEIGHT_TOO_SMALL = "height_under_8pct"
HEIGHT_TOO_LARGE = "height_over_22pct"
TOO_FAR_ABOVE_SUPPORT = "close_over_0.6atr_above_support"
THROUGH_SUPPORT = "close_over_0.25atr_through_support"
REASONS = (
    NO_BARS, SHORT_HISTORY, ADX_ABOVE_MAX, TOO_FEW_SUPPORT, TOO_FEW_RESISTANCE,
    HEIGHT_TOO_SMALL, HEIGHT_TOO_LARGE, TOO_FAR_ABOVE_SUPPORT, THROUGH_SUPPORT,
)

# Sequential stages for the funnel counts. Each stage keeps names with none of its codes.
STAGES = (
    ("strong_financials", ()),
    ("bars", (NO_BARS, SHORT_HISTORY)),
    ("adx_le_20", (ADX_ABOVE_MAX,)),
    ("touches_2_and_2", (TOO_FEW_SUPPORT, TOO_FEW_RESISTANCE)),
    ("height_8_to_22pct", (HEIGHT_TOO_SMALL, HEIGHT_TOO_LARGE)),
    ("at_support", (TOO_FAR_ABOVE_SUPPORT, THROUGH_SUPPORT)),
)

Q_RSI = "rsi_35_48"
Q_VOLUME = "volume_sma5_below_sma20"
Q_WICK = "lower_wick_40pct"
Q_NO_BREAK = "no_close_below_support_3"
QUALITY_CHECKS = (Q_RSI, Q_VOLUME, Q_WICK, Q_NO_BREAK)

POLICY = {
    "scan_name": SCAN_NAME,
    "financial_gate": STRONG,
    "feed": FEED,
    "adjustment": "split",
    "adx_period": ADX_PERIOD,
    "adx_max": SIDE_ADX_MAX,
    "channel_lookback_sessions": CHANNEL_LOOKBACK,
    "channel_height_pct": [CHANNEL_MIN_HEIGHT_PCT, CHANNEL_MAX_HEIGHT_PCT],
    "channel_height_base": "close",
    "channel_lines": "median of confirmed swing highs / lows with the extreme inside the lookback",
    "swing_method": "zigzag_high_low_confirmed_on_close",
    "swing_reversal_pct": SWING_REVERSAL_PCT,
    "min_touches_each_side": MIN_TOUCHES,
    "atr_period": ATR_PERIOD,
    "pivot_atr_max_above_support": PIVOT_ATR_MAX,
    "break_atr_max_below_support": BREAK_ATR_MAX,
    "min_bars": MIN_BARS,
    "recorded_not_required": ["sma50_slope", "sma_tangle", "quality_checks"],
    "sma50_slope_sessions": SLOPE_SESSIONS,
    "sma50_slope_max_pct": SLOPE_MAX_PCT,
    "rsi_period": RSI_PERIOD,
    "rsi_band": list(RSI_BAND),
    "volume_sma": [VOLUME_FAST, VOLUME_SLOW],
    "lower_wick_min": LOWER_WICK_MIN,
    "no_break_sessions": NO_BREAK_SESSIONS,
    "quality_min_checks_recorded": MIN_QUALITY_CHECKS,
    "lookback_calendar_days": LOOKBACK_CALENDAR_DAYS,
}


# --- one name ----------------------------------------------------------------

@dataclass(frozen=True)
class ChannelCall:
    passed: bool
    reasons: list[str]  # codes from REASONS; empty when the name passes
    detail: list[str]
    quality: dict[str, bool | None]  # recorded only; None when the inputs do not exist yet
    quality_passed: int
    metrics: dict


def check_channel(
    *, adx: float, support_touches: int, resistance_touches: int, height_pct: float | None,
    distance_atr: float | None,
) -> tuple[list[str], list[str]]:
    """Apply the rules to the measured numbers. Returns (reason codes, detail); no codes is a pass.

    ``height_pct`` and ``distance_atr`` are None when there is no channel to measure them on.
    """
    codes, detail = [], []
    if not adx <= SIDE_ADX_MAX:
        codes.append(ADX_ABOVE_MAX)
        detail.append(f"ADX {adx:.1f} is over {SIDE_ADX_MAX:g}")
    if support_touches < MIN_TOUCHES:
        codes.append(TOO_FEW_SUPPORT)
        detail.append(f"{support_touches} support touches in {CHANNEL_LOOKBACK} sessions, needs {MIN_TOUCHES}")
    if resistance_touches < MIN_TOUCHES:
        codes.append(TOO_FEW_RESISTANCE)
        detail.append(f"{resistance_touches} resistance touches in {CHANNEL_LOOKBACK} sessions, needs {MIN_TOUCHES}")
    if height_pct is not None:
        if height_pct < CHANNEL_MIN_HEIGHT_PCT:
            codes.append(HEIGHT_TOO_SMALL)
            detail.append(f"channel height {height_pct:.2f}% is under {CHANNEL_MIN_HEIGHT_PCT:g}%")
        if height_pct > CHANNEL_MAX_HEIGHT_PCT:
            codes.append(HEIGHT_TOO_LARGE)
            detail.append(f"channel height {height_pct:.2f}% is over {CHANNEL_MAX_HEIGHT_PCT:g}%")
    if distance_atr is not None:
        if distance_atr > PIVOT_ATR_MAX:
            codes.append(TOO_FAR_ABOVE_SUPPORT)
            detail.append(f"close is {distance_atr:.2f} ATR above support, over {PIVOT_ATR_MAX:g}")
        if distance_atr < -BREAK_ATR_MAX:
            codes.append(THROUGH_SUPPORT)
            detail.append(f"close is {-distance_atr:.2f} ATR through support, over {BREAK_ATR_MAX:g}")
    return codes, detail


def quality_checks(bars: pd.DataFrame, support: float | None, atr: float) -> tuple[dict[str, bool | None], dict]:
    """The four low-pivot quality checks on the last bar. Recorded, not required."""
    high, low, close, volume = bars["high"], bars["low"], bars["close"], bars["volume"].astype(float)
    rsi = float(wilder_rsi(close).iloc[-1])
    vol_fast, vol_slow = float(sma(volume, VOLUME_FAST).iloc[-1]), float(sma(volume, VOLUME_SLOW).iloc[-1])
    last = bars.iloc[-1]
    span = float(last["high"] - last["low"])
    lower_wick = (min(float(last["open"]), float(last["close"])) - float(last["low"])) / span if (
        "open" in bars and span > 0 and np.isfinite(last["open"])) else np.nan
    recent = close.tail(NO_BREAK_SESSIONS)

    quality = {
        Q_RSI: bool(RSI_BAND[0] <= rsi <= RSI_BAND[1]) if np.isfinite(rsi) else None,
        Q_VOLUME: bool(vol_fast < vol_slow) if np.isfinite(vol_fast) and np.isfinite(vol_slow) else None,
        Q_WICK: bool(lower_wick >= LOWER_WICK_MIN) if np.isfinite(lower_wick) else None,
        Q_NO_BREAK: bool((recent >= support).all()) if support is not None else None,
    }
    metrics = {
        f"rsi_{RSI_PERIOD}": _round(rsi),
        "volume_ratio_5_20": _round(vol_fast / vol_slow) if np.isfinite(vol_fast) and vol_slow > 0 else None,
        "lower_wick_fraction": _round(lower_wick),
    }
    return quality, metrics


def classify_long_channel(bars: pd.DataFrame) -> ChannelCall:
    """Check one symbol's bars. The caller has already dropped bars after as_of."""
    bars = bars.dropna(subset=["high", "low", "close"]).sort_values("date").reset_index(drop=True)
    count = len(bars)
    if count == 0:
        return ChannelCall(False, [NO_BARS], ["no daily bars on or before as_of"], {}, 0, {"bars": 0})
    last_bar = bars["date"].iloc[-1].date().isoformat()
    if count < MIN_BARS:
        return ChannelCall(False, [SHORT_HISTORY], [f"only {count} daily bars; needs {MIN_BARS}"], {}, 0,
                           {"bars": count, "last_bar_date": last_bar})

    high, low, close = bars["high"], bars["low"], bars["close"]
    last_close = float(close.iloc[-1])
    adx = float(wilder_adx(high, low, close, ADX_PERIOD).iloc[-1])
    atr = float(wilder_atr(high, low, close).iloc[-1])

    first_in_window = count - CHANNEL_LOOKBACK
    swings = [s for s in zigzag_swings(high, low, close) if s.index >= first_in_window]
    highs = [s for s in swings if s.kind == SWING_HIGH]
    lows = [s for s in swings if s.kind == SWING_LOW]
    resistance = float(np.median([s.price for s in highs])) if highs else None
    support = float(np.median([s.price for s in lows])) if lows else None
    height_pct = 100 * (resistance - support) / last_close if highs and lows else None
    distance_atr = (last_close - support) / atr if support is not None and np.isfinite(atr) and atr > 0 else None

    codes, detail = check_channel(
        adx=adx, support_touches=len(lows), resistance_touches=len(highs),
        height_pct=height_pct, distance_atr=distance_atr,
    )
    if not codes:
        detail = [
            f"ADX {adx:.1f} is at most {SIDE_ADX_MAX:g}",
            f"channel {support:.2f} to {resistance:.2f}, height {height_pct:.2f}% of close",
            f"{len(lows)} support and {len(highs)} resistance touches in {CHANNEL_LOOKBACK} sessions",
            f"close {last_close:.2f} is {distance_atr:+.2f} ATR from support",
        ]

    quality, quality_metrics = quality_checks(bars, support, atr)
    metrics = {
        "close": _round(last_close),
        f"adx_{ADX_PERIOD}": _round(adx),
        f"atr_{ATR_PERIOD}": _round(atr),
        "atr_pct": _round(100 * atr / last_close) if np.isfinite(atr) else None,
        "support": _round(support),
        "resistance": _round(resistance),
        "height_pct": _round(height_pct),
        "support_touches": [_touch(s, bars) for s in lows],
        "resistance_touches": [_touch(s, bars) for s in highs],
        "distance_to_support_atr": _round(distance_atr),
        **_recorded_trend_metrics(close),
        **quality_metrics,
        "bars": count,
        "last_bar_date": last_bar,
    }
    passed = not codes
    return ChannelCall(passed, codes, detail, quality, sum(1 for v in quality.values() if v), metrics)


def _recorded_trend_metrics(close: pd.Series) -> dict:
    """SMA50 slope and the 50/200 tangle. Recorded, not required."""
    fast = sma(close, FAST_WINDOW)
    out: dict = {"sma50_slope_pct": None, "sma50_slope_ok": None, "sma_spread_pct": None,
                 "sma_crossed_recently": None, "price_between_smas": None}
    if len(fast) > SLOPE_SESSIONS and np.isfinite(fast.iloc[-1]) and np.isfinite(fast.iloc[-1 - SLOPE_SESSIONS]):
        slope = 100 * (fast.iloc[-1] / fast.iloc[-1 - SLOPE_SESSIONS] - 1)
        out["sma50_slope_pct"] = _round(slope)
        out["sma50_slope_ok"] = bool(abs(slope) <= SLOPE_MAX_PCT)
    slow = sma(close, SLOW_WINDOW)
    if np.isfinite(slow.iloc[-1]):
        _, spread_pct, crossed = tangle_check(fast, slow)
        lo, hi = sorted((float(fast.iloc[-1]), float(slow.iloc[-1])))
        out["sma_spread_pct"] = _round(spread_pct)
        out["sma_crossed_recently"] = bool(crossed)
        out["price_between_smas"] = bool(lo <= float(close.iloc[-1]) <= hi)
    return out


def _touch(swing, bars: pd.DataFrame) -> dict:
    return {
        "date": bars["date"].iloc[swing.index].date().isoformat(),
        "price": _round(swing.price),
        "confirmed_on": bars["date"].iloc[swing.confirmed_index].date().isoformat(),
    }


def _round(value: float | None) -> float | None:
    return round(float(value), 4) if value is not None and np.isfinite(value) else None


# --- the run -----------------------------------------------------------------

@dataclass(frozen=True)
class LongChannelReport:
    """The SCAN-LongSidewaysChannel basket before ranking. Not an order list."""

    basket_id: str
    scan_name: str
    mode: str
    pivot_side: str
    as_of: date
    created_at: datetime
    financials_report: str
    financials_created_at: str
    policy: dict
    stage_counts: dict[str, int]  # survivors after each stage, in order
    reason_counts: dict[str, int]  # a name can carry several reasons
    quality_counts: dict[str, int]  # on members; recorded only
    members: list[str]
    calls: dict[str, dict]
    data_freshness: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    report_version: int = REPORT_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> LongChannelReport:
        if data.get("report_version") != REPORT_VERSION:
            raise ValueError(f"unsupported long channel report_version: {data.get('report_version')!r}")
        fields = dict(data)
        fields["as_of"] = date.fromisoformat(data["as_of"])
        fields["created_at"] = datetime.fromisoformat(data["created_at"])
        return cls(**fields)


def stage_counts(calls: dict[str, ChannelCall]) -> dict[str, int]:
    """Survivors after each stage, applied in order."""
    alive = set(calls)
    counts = {}
    for stage, codes in STAGES:
        alive = {s for s in alive if not set(calls[s].reasons) & set(codes)}
        counts[stage] = len(alive)
    return counts


def run_long_channel(
    financials: FinancialsReport,
    financials_path: str | Path,
    *,
    bars_fetcher: Callable = fetch_daily_bars,
    cache_dir: str | Path | None = None,
    created_at: datetime | None = None,
) -> LongChannelReport:
    """Check every STRONG survivor of the financials report. A bars failure raises and nothing is returned."""
    as_of = financials.as_of
    symbols = list(financials.gate_pass(STRONG).survivors)
    start = as_of - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    bars = _fetch_in_chunks(bars_fetcher, symbols, start, as_of, cache_dir) if symbols else pd.DataFrame()
    if not bars.empty:
        bars = bars[bars["date"] <= pd.Timestamp(as_of)]
    by_symbol = dict(tuple(bars.groupby("symbol"))) if not bars.empty else {}
    empty = pd.DataFrame({"date": pd.to_datetime([]), "open": [], "high": [], "low": [], "close": [], "volume": []})

    calls = {s: classify_long_channel(by_symbol.get(s, empty)) for s in sorted(symbols)}
    members = sorted(s for s, c in calls.items() if c.passed)
    counts = stage_counts(calls)
    if counts["strong_financials"] != len(symbols) or counts[STAGES[-1][0]] != len(members):
        raise AssertionError(f"long channel lost track of names: {len(symbols)} in, {counts}, {len(members)} members")

    warnings = [w for w in financials.warnings if "point in time" in w]
    last_dates = {s: c.metrics.get("last_bar_date") for s, c in calls.items() if c.metrics.get("last_bar_date")}
    newest = max(last_dates.values()) if last_dates else None
    if newest:
        stale = sorted(s for s, d in last_dates.items() if d < newest)
        if stale:
            warnings.append(f"{len(stale)} names have no bar on {newest}: {', '.join(stale)}")

    return LongChannelReport(
        basket_id=f"{SCAN_NAME}-{as_of.isoformat()}",
        scan_name=SCAN_NAME,
        mode=MODE,
        pivot_side=PIVOT_SIDE,
        as_of=as_of,
        created_at=created_at or datetime.now(UTC),
        financials_report=str(financials_path),
        financials_created_at=financials.created_at.isoformat(),
        policy=dict(POLICY),
        stage_counts=counts,
        reason_counts={code: sum(1 for c in calls.values() if code in c.reasons) for code in REASONS},
        quality_counts={q: sum(1 for s in members if calls[s].quality.get(q)) for q in QUALITY_CHECKS},
        members=members,
        calls={s: asdict(c) for s, c in calls.items()},
        data_freshness={"bars": newest or "", "financials_created_at": financials.created_at.isoformat()},
        warnings=warnings,
    )


def save_report(report: LongChannelReport, out_dir: str | Path | None = None) -> Path:
    """Write the report as a new file and return its path. An existing report is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_report_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"long_channel_{report.as_of}_{report.created_at:%Y%m%dT%H%M%SZ}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2)
        fh.write("\n")
    return path


def load_report(path: str | Path) -> LongChannelReport:
    return LongChannelReport.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
