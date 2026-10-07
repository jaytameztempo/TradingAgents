"""SCANBot build step 3: the trend classifier, on the STRONG financial survivors.

Every STRONG survivor of a saved financials report gets one label from split-
adjusted SIP daily bars dated on or before as_of, with RegimeBot's windows and
cutoffs:

- UP if close > SMA50 > SMA200 and ADX(14) >= 25
- DOWN if close < SMA50 < SMA200 and ADX(14) >= 25
- UNCLASSIFIED otherwise, with every reason that applies:
  no bars, fewer than 200 bars, the 50-day within 1% of the 200-day, the two
  crossed in the last 20 sessions, price not aligned, or ADX under 25.

A tangled pair of averages leaves a name unclassified even when price is
aligned and ADX is at least 25, as RegimeBot would call it SIDE. Unclassified
is not a channel scan: this step builds no slope or swing rules, pivots,
channel scans, ranking or baskets. Nothing here places an order.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

from extensions.market_data.alpaca_bars import fetch_daily_bars
from extensions.regime.indicators import sma, wilder_adx
from extensions.regime.regime_bot import (
    ADX_PERIOD,
    ADX_TREND_MIN,
    FAST_WINDOW,
    LOOKBACK_CALENDAR_DAYS,
    MIN_BARS,
    SLOW_WINDOW,
    TANGLE_CROSS_DAYS,
    TANGLE_SPREAD_PCT,
    tangle_check,
)
from extensions.scanbot.financials import STRONG, FinancialsReport
from extensions.scanbot.funnel import FEED, _fetch_in_chunks, default_report_dir

REPORT_VERSION = 1
UP, DOWN, UNCLASSIFIED = "UP", "DOWN", "UNCLASSIFIED"
LABELS = (UP, DOWN, UNCLASSIFIED)

NO_BARS = "no_bars"
SHORT_HISTORY = "short_history"
SMA_WITHIN_SPREAD = "sma_within_1pct"
SMA_CROSSED = "sma_crossed_recently"
NOT_ALIGNED = "not_aligned"
ADX_BELOW_MIN = "adx_below_25"
REASONS = (NO_BARS, SHORT_HISTORY, SMA_WITHIN_SPREAD, SMA_CROSSED, NOT_ALIGNED, ADX_BELOW_MIN)

POLICY = {
    "financial_gate": STRONG,
    "feed": FEED,
    "adjustment": "split",
    "fast_window": FAST_WINDOW,
    "slow_window": SLOW_WINDOW,
    "min_bars": MIN_BARS,
    "adx_period": ADX_PERIOD,
    "adx_trend_min": ADX_TREND_MIN,
    "tangle_spread_pct": TANGLE_SPREAD_PCT,
    "tangle_cross_sessions": TANGLE_CROSS_DAYS,
    "lookback_calendar_days": LOOKBACK_CALENDAR_DAYS,
}


@dataclass(frozen=True)
class TrendCall:
    label: str
    reasons: list[str]  # codes from REASONS; empty for UP and DOWN
    detail: list[str]
    metrics: dict


def decide_trend(
    *, close: float, sma_fast: float, sma_slow: float, adx: float, spread_pct: float, crossed: bool
) -> tuple[str, list[str], list[str]]:
    """Apply the label rules to the latest numbers. Returns (label, reason codes, detail)."""
    codes, detail = [], []
    if abs(spread_pct) < TANGLE_SPREAD_PCT:
        codes.append(SMA_WITHIN_SPREAD)
        detail.append(f"{FAST_WINDOW}-day is {spread_pct:+.2f}% from the {SLOW_WINDOW}-day, within {TANGLE_SPREAD_PCT:g}%")
    if crossed:
        codes.append(SMA_CROSSED)
        detail.append(f"{FAST_WINDOW}-day and {SLOW_WINDOW}-day crossed in the last {TANGLE_CROSS_DAYS} sessions")

    aligned_up = close > sma_fast > sma_slow
    aligned_down = close < sma_fast < sma_slow
    numbers = f"close {close:.2f}, {FAST_WINDOW}-day {sma_fast:.2f}, {SLOW_WINDOW}-day {sma_slow:.2f}"
    if not (aligned_up or aligned_down):
        codes.append(NOT_ALIGNED)
        detail.append(f"{numbers}: neither aligned up nor aligned down")
    if not adx >= ADX_TREND_MIN:
        codes.append(ADX_BELOW_MIN)
        detail.append(f"ADX {adx:.1f} is under {ADX_TREND_MIN:g}")

    if codes:
        return UNCLASSIFIED, codes, detail
    direction = "up" if aligned_up else "down"
    detail = [f"{numbers}: aligned {direction}", f"ADX {adx:.1f} is at least {ADX_TREND_MIN:g}"]
    return (UP if aligned_up else DOWN), [], detail


def classify_trend(bars: pd.DataFrame) -> TrendCall:
    """Label one symbol's bars. The caller has already dropped bars after as_of."""
    bars = bars.dropna(subset=["high", "low", "close"]).sort_values("date").reset_index(drop=True)
    count = len(bars)
    if count == 0:
        return TrendCall(UNCLASSIFIED, [NO_BARS], ["no daily bars on or before as_of"], {"bars": 0})
    if count < MIN_BARS:
        return TrendCall(
            UNCLASSIFIED, [SHORT_HISTORY], [f"only {count} daily bars; needs {MIN_BARS}"],
            {"bars": count, "last_bar_date": bars["date"].iloc[-1].date().isoformat()},
        )

    fast_series, slow_series = sma(bars["close"], FAST_WINDOW), sma(bars["close"], SLOW_WINDOW)
    adx = float(wilder_adx(bars["high"], bars["low"], bars["close"], ADX_PERIOD).iloc[-1])
    close, fast, slow = float(bars["close"].iloc[-1]), float(fast_series.iloc[-1]), float(slow_series.iloc[-1])
    _, spread_pct, crossed = tangle_check(fast_series, slow_series)

    label, codes, detail = decide_trend(
        close=close, sma_fast=fast, sma_slow=slow, adx=adx, spread_pct=spread_pct, crossed=crossed
    )
    metrics = {
        "close": round(close, 4),
        f"sma_{FAST_WINDOW}": round(fast, 4),
        f"sma_{SLOW_WINDOW}": round(slow, 4),
        "sma_spread_pct": round(spread_pct, 4),
        "crossed_recently": crossed,
        f"adx_{ADX_PERIOD}": round(adx, 4),
        "bars": count,
        "last_bar_date": bars["date"].iloc[-1].date().isoformat(),
    }
    return TrendCall(label, codes, detail, metrics)


@dataclass(frozen=True)
class TrendReport:
    as_of: date
    created_at: datetime
    financials_report: str
    financials_created_at: str
    policy: dict
    entered: int
    label_counts: dict[str, int]
    reason_counts: dict[str, int]  # a name can carry several reasons
    tangled_only: int  # aligned and ADX >= 25, unclassified only because the averages are tangled
    up: list[str]
    down: list[str]
    classifications: dict[str, dict]
    warnings: list[str] = field(default_factory=list)
    report_version: int = REPORT_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> TrendReport:
        if data.get("report_version") != REPORT_VERSION:
            raise ValueError(f"unsupported trend report_version: {data.get('report_version')!r}")
        fields = dict(data)
        fields["as_of"] = date.fromisoformat(data["as_of"])
        fields["created_at"] = datetime.fromisoformat(data["created_at"])
        return cls(**fields)


def run_trend(
    financials: FinancialsReport,
    financials_path: str | Path,
    *,
    bars_fetcher: Callable = fetch_daily_bars,
    cache_dir: str | Path | None = None,
    created_at: datetime | None = None,
) -> TrendReport:
    """Classify every STRONG survivor of the financials report. A bars failure raises and nothing is returned."""
    as_of = financials.as_of
    symbols = list(financials.gate_pass(STRONG).survivors)
    start = as_of - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    bars = _fetch_in_chunks(bars_fetcher, symbols, start, as_of, cache_dir) if symbols else pd.DataFrame()
    if not bars.empty:
        bars = bars[bars["date"] <= pd.Timestamp(as_of)]
    by_symbol = dict(tuple(bars.groupby("symbol"))) if not bars.empty else {}
    empty = pd.DataFrame({"date": pd.to_datetime([]), "high": [], "low": [], "close": []})

    calls = {s: classify_trend(by_symbol.get(s, empty)) for s in symbols}
    label_counts = {label: sum(1 for c in calls.values() if c.label == label) for label in LABELS}
    if sum(label_counts.values()) != len(symbols):
        raise AssertionError(f"trend labels lost track of names: {len(symbols)} in, {label_counts}")
    reason_counts = {code: sum(1 for c in calls.values() if code in c.reasons) for code in REASONS}
    tangled_only = sum(
        1 for c in calls.values()
        if c.reasons and set(c.reasons) <= {SMA_WITHIN_SPREAD, SMA_CROSSED}
    )

    warnings = [w for w in financials.warnings if "point in time" in w]
    last_dates = {s: c.metrics.get("last_bar_date") for s, c in calls.items() if c.metrics.get("last_bar_date")}
    if last_dates:
        newest = max(last_dates.values())
        stale = sorted(s for s, d in last_dates.items() if d < newest)
        if stale:
            warnings.append(f"{len(stale)} names have no bar on {newest}: {', '.join(stale)}")

    return TrendReport(
        as_of=as_of,
        created_at=created_at or datetime.now(UTC),
        financials_report=str(financials_path),
        financials_created_at=financials.created_at.isoformat(),
        policy=dict(POLICY),
        entered=len(symbols),
        label_counts=label_counts,
        reason_counts=reason_counts,
        tangled_only=tangled_only,
        up=sorted(s for s, c in calls.items() if c.label == UP),
        down=sorted(s for s, c in calls.items() if c.label == DOWN),
        classifications={s: asdict(c) for s, c in sorted(calls.items())},
        warnings=warnings,
    )


def save_report(report: TrendReport, out_dir: str | Path | None = None) -> Path:
    """Write the report as a new file and return its path. An existing report is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_report_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"trend_{report.as_of}_{report.created_at:%Y%m%dT%H%M%SZ}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2)
        fh.write("\n")
    return path


def load_report(path: str | Path) -> TrendReport:
    return TrendReport.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
