"""SCANSIDE by category: sideways channels, long at support and short at resistance, at three strictness levels.

The rules come from ``categories.SIDE`` (Category 1 strictest, 3 loosest); this
module only applies the SideParams it is given. One run writes two baskets.

The channel is the SCANBot channel scans' own: confirmed ZigZag swings (5%
reversal) whose extreme bar is inside the last 60 sessions, support the lower
of the last two swing lows and resistance the higher of the last two swing
highs (``swings.channel_lines``). Touches count every swing of its kind in the
window. Height is (resistance - support) / close.

Each side, in order:

- candidates: the universe report's liquidity survivors in the side's financial
  groups (long Cat 1 STRONG, Cat 2 STRONG or NEUTRAL; short Cat 1 WEAK), or all
  of them when the category has no financial filter
- at least 60 bars
- ADX(14) at most the ceiling
- channel height inside the band, and at least the minimum touches each side
- the signed ATR distance from the line inside the band: (close - support) / ATR
  for a long, (resistance - close) / ATR for a short. Cat 1 and 2 bands start
  at 0, so a close through the line fails; Cat 3's band is -1.5 to 1.5.
- RSI(14) inside the side's band
- as-of volume below the volume ratio x the 20-day average (Cat 1 and 2)
- shorts only, borrow on the asset snapshot: easy_to_borrow (Cat 1 and 2),
  shortable or easy_to_borrow (Cat 3)

The pivot confirmation and the TTM Squeeze are not run: they stay later,
optional steps. Every failed rule is recorded. Each basket is a TickerBasket,
scored by closeness to the line. Only bars dated on or before as_of are used.
Nothing here places an order.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from extensions.market_data.alpaca_bars import fetch_daily_bars
from extensions.scanbot.long_channel import CHANNEL_LOOKBACK
from extensions.scanbot.swings import SWING_HIGH, SWING_LOW, channel_lines
from extensions.scans.basket import BasketMember, Rejection, TickerBasket
from extensions.scanbot.financials import STRONG, WEAK
from extensions.scans.categories import NEUTRAL, SideParams
from extensions.scans.category_inputs import (
    Measures,
    ScanInputs,
    basket_parameters,
    borrow_ok,
    borrow_text,
    fetch_bars,
    in_band,
    measure,
    rnd,
    stale_warning,
)

REGIME = "SIDE"
LONG, SHORT = "long", "short"
SIDES = (LONG, SHORT)
MIN_BARS = CHANNEL_LOOKBACK
SCORE = "minus the absolute ATR distance from the line (support for longs, resistance for shorts)"


def scan_name(side: str, category: int) -> str:
    return f"SCAN-SIDE {side.capitalize()} Category {category}"


def channel(m: Measures) -> dict:
    """Support, resistance, touches, height and the signed ATR distance from each line."""
    first_in_window = m.bars - CHANNEL_LOOKBACK
    swings = [s for s in m.swings if s.index >= first_in_window]
    support, resistance, _, _ = channel_lines(swings)
    atr_ok = np.isfinite(m.atr) and m.atr > 0
    return {
        "support": support,
        "resistance": resistance,
        "support_touches": sum(1 for s in swings if s.kind == SWING_LOW),
        "resistance_touches": sum(1 for s in swings if s.kind == SWING_HIGH),
        "height_pct": 100 * (resistance - support) / m.close if support is not None and resistance is not None else None,
        "distance_long_atr": (m.close - support) / m.atr if support is not None and atr_ok else None,
        "distance_short_atr": (resistance - m.close) / m.atr if resistance is not None and atr_ok else None,
    }


def check_side(side: str, m: Measures, ch: dict, flags: dict | None, p: SideParams) -> list[str]:
    """Apply one side's rules. Returns the failed rules as text; none is a pass."""
    reasons = []
    if side == SHORT and not borrow_ok(p.short_borrow, flags):
        reasons.append(borrow_text(p.short_borrow, flags))
    if m.bars < MIN_BARS:
        return [f"only {m.bars} daily bars; needs {MIN_BARS}", *reasons]
    if not m.adx <= p.adx_max:
        reasons.append(f"ADX {m.adx:.1f} is over {p.adx_max:g}")
    if ch["support_touches"] < p.min_touches:
        reasons.append(f"{ch['support_touches']} support touches in {CHANNEL_LOOKBACK} sessions, needs {p.min_touches}")
    if ch["resistance_touches"] < p.min_touches:
        reasons.append(f"{ch['resistance_touches']} resistance touches in {CHANNEL_LOOKBACK} sessions, "
                       f"needs {p.min_touches}")
    if ch["height_pct"] is not None and not in_band(ch["height_pct"], p.height_pct):
        reasons.append(f"channel height {ch['height_pct']:.2f}% is outside {p.height_pct[0]:g}-{p.height_pct[1]:g}%")

    line = "support" if side == LONG else "resistance"
    distance = ch["distance_long_atr"] if side == LONG else ch["distance_short_atr"]
    if distance is not None and not in_band(distance, p.band_atr):
        where = "above" if side == LONG else "below"
        reasons.append(f"close is {distance:+.2f} ATR {where} {line}, outside {p.band_atr[0]:g} to {p.band_atr[1]:g}")

    rsi_band = p.rsi_long if side == LONG else p.rsi_short
    if not in_band(m.rsi, rsi_band):
        reasons.append(f"RSI {m.rsi:.1f} is outside {rsi_band[0]:g}-{rsi_band[1]:g}")
    if p.volume_below is not None and not m.volume < p.volume_below * m.volume_avg20:
        reasons.append(f"as-of volume {m.volume:,.0f} is not below {p.volume_below:g}x the 20-day average "
                       f"{m.volume_avg20:,.0f}")
    return reasons


def candidates(inputs: ScanInputs, groups: tuple[str, ...] | None) -> list[str]:
    if groups is None:
        return list(inputs.universe)
    chosen = set().union(*(inputs.group(g) for g in groups))
    return sorted(s for s in inputs.universe if s in chosen)


def build_baskets(
    inputs: ScanInputs, params: SideParams, by_symbol: dict[str, pd.DataFrame],
    created_at: datetime | None = None,
) -> dict[str, TickerBasket]:
    """Apply each side's rules to its candidates. Returns {"long": basket, "short": basket}."""
    created_at = created_at or datetime.now(UTC)
    groups = {LONG: params.long_financials, SHORT: params.short_financials}
    names = {side: candidates(inputs, groups[side]) for side in SIDES}
    measured = sorted(set(names[LONG]) | set(names[SHORT]))
    measures = {s: measure(by_symbol[s]) if s in by_symbol else None for s in measured}
    channels = {s: channel(m) for s, m in measures.items() if m is not None and m.bars >= MIN_BARS}
    warnings = stale_warning(measures)

    baskets = {}
    for side in SIDES:
        members, rejected = [], []
        for s in names[side]:
            m, flags = measures[s], inputs.borrow.get(s)
            if m is None:
                borrow = ([borrow_text(params.short_borrow, flags)]
                          if side == SHORT and not borrow_ok(params.short_borrow, flags) else [])
                rejected.append(Rejection(s, "; ".join(["no bars", *borrow])))
                continue
            ch = channels.get(s) or {"support_touches": 0, "resistance_touches": 0, "height_pct": None,
                                     "distance_long_atr": None, "distance_short_atr": None,
                                     "support": None, "resistance": None}
            reasons = check_side(side, m, ch, flags, params)
            if reasons:
                rejected.append(Rejection(s, "; ".join(reasons)))
                continue
            distance = ch["distance_long_atr"] if side == LONG else ch["distance_short_atr"]
            metrics = {**m.metrics(), **{k: rnd(v) if isinstance(v, float) else v for k, v in ch.items()},
                       "financial_group": _group(inputs, s)}
            if side == SHORT:
                metrics.update(shortable=(flags or {}).get("shortable"), easy_to_borrow=(flags or {}).get("easy_to_borrow"))
            members.append(BasketMember(s, round(-abs(distance), 6), metrics))
        members.sort(key=lambda b: b.score, reverse=True)
        gate = groups[side]
        baskets[side] = TickerBasket(
            scan_name=scan_name(side, params.category),
            as_of=inputs.as_of,
            created_at=created_at,
            universe=names[side],
            parameters=basket_parameters(
                inputs, regime=REGIME, side=side, params=params,
                financial_gate=" or ".join(gate) if gate else None, rs_population=0, score=SCORE,
                warnings=warnings,
            ) | {"channel_lookback_sessions": CHANNEL_LOOKBACK, "min_bars": MIN_BARS,
                 "pivot_confirmation": "not run", "ttm_squeeze": "not run"},
            members=members,
            rejected=rejected,
        )
    return baskets


def run_scan(
    inputs: ScanInputs, params: SideParams, *, bars_fetcher: Callable = fetch_daily_bars,
    cache_dir: str | Path | None = None, created_at: datetime | None = None,
) -> dict[str, TickerBasket]:
    """Fetch read-only daily bars for both sides' candidates (no SPY: SCANSIDE has no RS rule), then build
    both baskets. A bars failure raises."""
    symbols = set().union(*(candidates(inputs, g) for g in (params.long_financials, params.short_financials)))
    by_symbol, _ = fetch_bars(inputs, symbols, benchmark=False, bars_fetcher=bars_fetcher, cache_dir=cache_dir)
    return build_baskets(inputs, params, by_symbol, created_at)


def _group(inputs: ScanInputs, symbol: str) -> str | None:
    for name in (STRONG, WEAK, NEUTRAL):
        if symbol in inputs.group(name):
            return name
    return None
