"""SCAN-Breakdown Short Candidates: keep names in a simple moving-average downtrend.

A ticker passes when all three hold on the scan date:

1. at least 200 daily bars, so the 200-day average is real;
2. the last close is strictly below the 50-day simple moving average;
3. the 50-day average is strictly below the 200-day average.

Only bars dated on or before the scan date are used, so a past scan cannot
see later prices. The SCAN reads bars and writes a basket. It places no orders,
and nothing routes its basket yet.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from extensions.market_data.alpaca_bars import (
    fetch_daily_bars,
    validate_date_range,
    validate_ticker,
)
from extensions.scans.basket import BasketMember, Rejection, TickerBasket
from extensions.scans.upward_trend_momentum import DEFAULT_UNIVERSE

__all__ = ["DEFAULT_UNIVERSE", "SCAN_NAME", "CheckResult", "build_basket", "check_downtrend", "run_scan"]

SCAN_NAME = "SCAN-Breakdown Short Candidates"
FAST_WINDOW = 50
SLOW_WINDOW = 200
MIN_BARS = 200
# About 275 trading days: enough for a 200-day average with room for holidays.
LOOKBACK_CALENDAR_DAYS = 400


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    reasons: list[str]
    bars: int
    close: float | None = None
    sma_fast: float | None = None
    sma_slow: float | None = None


def check_downtrend(closes: pd.Series) -> CheckResult:
    """Apply the three checks to closes in date order, oldest first."""
    closes = closes.dropna()
    bars = len(closes)
    if bars < MIN_BARS:
        return CheckResult(False, [f"only {bars} daily bars; needs {MIN_BARS}"], bars)

    close = float(closes.iloc[-1])
    sma_fast = float(closes.iloc[-FAST_WINDOW:].mean())
    sma_slow = float(closes.iloc[-SLOW_WINDOW:].mean())
    reasons = []
    if not close < sma_fast:
        reasons.append(f"close {close:.2f} is not below the {FAST_WINDOW}-day average {sma_fast:.2f}")
    if not sma_fast < sma_slow:
        reasons.append(
            f"{FAST_WINDOW}-day average {sma_fast:.2f} is not below the {SLOW_WINDOW}-day average {sma_slow:.2f}"
        )
    return CheckResult(not reasons, reasons, bars, close, sma_fast, sma_slow)


def build_basket(
    bars: pd.DataFrame,
    universe: Iterable[str],
    as_of: date,
    created_at: datetime | None = None,
) -> TickerBasket:
    """Run the checks over a bars frame (as fetch_daily_bars returns it) and build the basket."""
    universe = list(dict.fromkeys(validate_ticker(t) for t in universe))
    created_at = created_at or datetime.now(UTC)
    visible = bars[bars["date"] <= pd.Timestamp(as_of)]

    members, rejected = [], []
    for ticker in universe:
        rows = visible[visible["symbol"] == ticker].sort_values("date")
        if rows.empty:
            rejected.append(Rejection(ticker, "no bars"))
            continue
        result = check_downtrend(rows["close"].reset_index(drop=True))
        if not result.passed:
            rejected.append(Rejection(ticker, "; ".join(result.reasons)))
            continue
        members.append(
            BasketMember(
                ticker=ticker,
                score=round(1 - result.close / result.sma_fast, 6),
                metrics={
                    "close": round(result.close, 4),
                    f"sma_{FAST_WINDOW}": round(result.sma_fast, 4),
                    f"sma_{SLOW_WINDOW}": round(result.sma_slow, 4),
                    "bars": result.bars,
                    "last_bar_date": rows["date"].iloc[-1].date().isoformat(),
                },
            )
        )

    members.sort(key=lambda m: m.score, reverse=True)
    return TickerBasket(
        scan_name=SCAN_NAME,
        as_of=as_of,
        created_at=created_at,
        universe=universe,
        parameters={
            "fast_window": FAST_WINDOW,
            "slow_window": SLOW_WINDOW,
            "min_bars": MIN_BARS,
            "lookback_calendar_days": LOOKBACK_CALENDAR_DAYS,
            "score": f"1 - close / sma_{FAST_WINDOW}",
        },
        members=members,
        rejected=rejected,
    )


def run_scan(
    as_of: date | str,
    universe: Iterable[str] = DEFAULT_UNIVERSE,
    *,
    client=None,
    cache_dir=None,
) -> TickerBasket:
    """Fetch read-only daily bars ending on the scan date and build the basket."""
    universe = list(dict.fromkeys(validate_ticker(t) for t in universe))
    _, as_of = validate_date_range(as_of, as_of)
    start = as_of - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    bars = fetch_daily_bars(universe, start, as_of, client=client, cache_dir=cache_dir)
    return build_basket(bars, universe, as_of)
