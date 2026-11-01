"""SCANUP by category: STRONG financial survivors in an uptrend, at one of three strictness levels.

The rules come from ``categories.UP`` (Category 1 strictest, 3 loosest); this
module only applies the UpParams it is given. The candidates are the STRONG
survivors of a financials report, as in the SCANBot trend step. In order:

- enough bars (200, or 64 when SMA200 is not used)
- close > SMA50 > SMA200 (Cat 1 and 2) or close > SMA50 (Cat 3), and SMA50 rising (Cat 1)
- ADX(14) at least the floor
- RS rank at least the floor; on Cat 1, 63-session strength vs SPY above 0 also passes
- pullback below the last confirmed ZigZag high inside the band (Cat 1 and 2),
  on declining volume (Cat 1: 5-session average below the 20-day average)
- RSI(14) inside the band
- close within the ATR limit of any proximity level (Cat 1: SMA20 or the last
  swing low, 1 ATR; Cat 3: SMA20 or SMA50, 2 ATR)

Every failed rule is recorded. The basket is a TickerBasket, scored by strength
vs SPY. Only bars dated on or before as_of are used. Nothing here places an order.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from extensions.market_data.alpaca_bars import fetch_daily_bars
from extensions.scanbot.financials import STRONG
from extensions.scanbot.swings import SWING_HIGH
from extensions.scans.basket import BasketMember, Rejection, TickerBasket
from extensions.scans.categories import UpParams
from extensions.scans.category_inputs import (
    EDGE,
    Measures,
    ScanInputs,
    basket_parameters,
    fetch_bars,
    in_band,
    measure,
    relative_strength,
    rnd,
    stale_warning,
)

REGIME, SIDE = "UP", "long"
SCORE = "63-session return minus SPY's"


def scan_name(category: int) -> str:
    return f"SCAN-UP Category {category}"


def check_up(m: Measures, rs: dict, p: UpParams) -> tuple[list[str], dict]:
    """Apply the rules to one name's measures. Returns (failed rules as text, extra metrics); no text is a pass."""
    if m.bars < p.min_bars:
        return [f"only {m.bars} daily bars; needs {p.min_bars}"], {}
    reasons, extra = [], {}
    if p.above_sma200:
        if not m.close > m.sma50 > m.sma200:
            reasons.append(f"close {m.close:.2f} > SMA50 {m.sma50:.2f} > SMA200 {m.sma200:.2f} does not hold")
    elif not m.close > m.sma50:
        reasons.append(f"close {m.close:.2f} is not above SMA50 {m.sma50:.2f}")
    if p.sma50_rising and not m.sma50 > m.sma50_prior:
        reasons.append(f"SMA50 {m.sma50:.2f} is not above its value 20 bars ago {m.sma50_prior:.2f}")
    if not m.adx >= p.adx_min:
        reasons.append(f"ADX {m.adx:.1f} is under {p.adx_min:g}")

    if p.rs_rank_min is not None:
        rank, strength = rs.get("rs_rank"), rs.get("strength_vs_spy")
        ok = rank is not None and rank >= p.rs_rank_min
        if p.rs_or_beats_spy:
            ok = ok or (strength is not None and strength > 0)
        if not ok:
            spy = f" and strength vs SPY {_pct(strength)} is not above 0" if p.rs_or_beats_spy else ""
            reasons.append(f"RS rank {_num(rank)} is under {p.rs_rank_min:g}{spy}")

    if p.pullback_pct is not None:
        high = m.last_swing(SWING_HIGH)
        if high is None:
            reasons.append("no confirmed ZigZag swing high to measure the pullback from")
        else:
            pullback = 100 * (high.price - m.close) / high.price
            extra["pullback_pct"] = rnd(pullback)
            if not in_band(pullback, p.pullback_pct):
                reasons.append(f"pullback {pullback:.2f}% below the last swing high {high.price:.2f} is outside "
                               f"{p.pullback_pct[0]:g}-{p.pullback_pct[1]:g}%")
    if p.pullback_declining_volume and not m.volume_avg5 < m.volume_avg20:
        reasons.append(f"5-session average volume {m.volume_avg5:,.0f} is not below the 20-day average "
                       f"{m.volume_avg20:,.0f}")

    if not in_band(m.rsi, p.rsi):
        reasons.append(f"RSI {m.rsi:.1f} is outside {p.rsi[0]:g}-{p.rsi[1]:g}")

    if p.near_atr_max is not None:
        distances = {level: m.atr_distance(m.level(level)) for level in p.near_levels}
        extra["distance_atr"] = {level: rnd(d) for level, d in distances.items()}
        if not any(d is not None and d <= p.near_atr_max + EDGE for d in distances.values()):
            shown = ", ".join(f"{level} {_num(d)}" for level, d in distances.items())
            reasons.append(f"close is not within {p.near_atr_max:g} ATR of {' or '.join(p.near_levels)} ({shown})")
    return reasons, extra


def build_basket(
    inputs: ScanInputs, params: UpParams, by_symbol: dict[str, pd.DataFrame], spy: pd.DataFrame,
    created_at: datetime | None = None,
) -> TickerBasket:
    """Apply the rules to every STRONG survivor and build the basket."""
    candidates = sorted(inputs.strong)
    rs = relative_strength(by_symbol, spy, inputs.universe)
    measures = {s: measure(by_symbol[s]) if s in by_symbol else None for s in candidates}

    members, rejected = [], []
    for s in candidates:
        m = measures[s]
        if m is None:
            rejected.append(Rejection(s, "no bars"))
            continue
        reasons, extra = check_up(m, rs[s], params)
        if reasons:
            rejected.append(Rejection(s, "; ".join(reasons)))
            continue
        strength = rs[s]["strength_vs_spy"]
        members.append(BasketMember(s, round(strength, 6) if strength is not None else 0.0,
                                    {**m.metrics(), **rs[s], **extra}))
    members.sort(key=lambda b: b.score, reverse=True)

    return TickerBasket(
        scan_name=scan_name(params.category),
        as_of=inputs.as_of,
        created_at=created_at or datetime.now(UTC),
        universe=candidates,
        parameters=basket_parameters(
            inputs, regime=REGIME, side=SIDE, params=params, financial_gate=STRONG,
            rs_population=sum(1 for v in rs.values() if v["rs_rank"] is not None), score=SCORE,
            warnings=stale_warning(measures),
        ),
        members=members,
        rejected=rejected,
    )


def run_scan(
    inputs: ScanInputs, params: UpParams, *, bars_fetcher: Callable = fetch_daily_bars,
    cache_dir: str | Path | None = None, created_at: datetime | None = None,
) -> TickerBasket:
    """Fetch read-only daily bars for the candidates and SPY, then build the basket. A bars failure raises."""
    by_symbol, spy = fetch_bars(inputs, bars_fetcher=bars_fetcher, cache_dir=cache_dir)
    return build_basket(inputs, params, by_symbol, spy, created_at)


def _num(value) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _pct(value) -> str:
    return "n/a" if value is None else f"{100 * value:+.2f}%"
