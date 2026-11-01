"""SCANDOWN by category: STRONG financial survivors in a downtrend, at one of three strictness levels.

The rules come from ``categories.DOWN`` (Category 1 strictest, 3 loosest); this
module only applies the DownParams it is given. The candidates are the STRONG
survivors of a financials report, as in the SCANBot trend step. In order:

- enough bars (200, or 64 when SMA200 is not used)
- close < SMA50 < SMA200 (Cat 1 and 2) or close < SMA50 (Cat 3)
- ADX(14) at least the floor
- RS rank at most the ceiling; on Cat 1, 63-session strength vs SPY below 0 also passes
- the structure:
  - Cat 1, failed rally: a bar in the 5 sessions before as-of reached SMA20 or
    broken support (its high at or above the level) on volume below the 20-day
    average, and the as-of bar closed back below that level on at least 1.5x
    the 20-day average. Broken support is the latest confirmed ZigZag swing low
    that a close fell below after it was confirmed and before the rally bar.
  - Cat 2: close below the last confirmed ZigZag swing high, or below SMA20
  - Cat 3: close below SMA20, or below SMA50
- RSI(14) inside the band
- borrow on the asset snapshot: shortable and easy_to_borrow (Cat 1),
  easy_to_borrow (Cat 2), shortable or easy_to_borrow (Cat 3)

Every failed rule is recorded. The basket is a TickerBasket, scored by weakness
vs SPY. Only bars dated on or before as_of are used. Nothing here places an order.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from extensions.market_data.alpaca_bars import fetch_daily_bars
from extensions.scanbot.financials import STRONG
from extensions.scanbot.swings import SWING_HIGH, SWING_LOW, Swing
from extensions.scans.basket import BasketMember, Rejection, TickerBasket
from extensions.scans.categories import (
    BELOW_SMA20_OR_SMA50,
    BELOW_SWING_HIGH_OR_SMA20,
    FAILED_RALLY,
    DownParams,
)
from extensions.scans.category_inputs import (
    Measures,
    ScanInputs,
    basket_parameters,
    borrow_ok,
    borrow_text,
    fetch_bars,
    in_band,
    measure,
    relative_strength,
    rnd,
    stale_warning,
)

REGIME, SIDE = "DOWN", "short"
SCORE = "SPY's 63-session return minus the name's"


def scan_name(category: int) -> str:
    return f"SCAN-DOWN Category {category}"


def broken_support(close: np.ndarray, swings: list[Swing], before: int) -> float | None:
    """The latest confirmed swing low that a close fell below after its confirmation and before bar ``before``."""
    for swing in sorted((s for s in swings if s.kind == SWING_LOW), key=lambda s: s.index, reverse=True):
        after = close[swing.confirmed_index + 1:before]
        if swing.confirmed_index < before and len(after) and (after < swing.price).any():
            return swing.price
    return None


def failed_rally(m: Measures, p: DownParams) -> dict | None:
    """The latest rally bar that failed into SMA20 or broken support, or None. The as-of bar's volume is not
    checked here."""
    high, close, volume, sma20 = (m.series[k] for k in ("high", "close", "volume", "sma20"))
    last = len(close) - 1
    avg = m.volume_avg20
    if not np.isfinite(avg) or avg <= 0:
        return None
    for j in range(last - 1, max(last - 1 - p.rally_sessions, -1), -1):
        if not volume[j] < p.rally_volume_below * avg:
            continue
        support = broken_support(close, m.swings, j)
        levels = (("sma20", sma20[j], sma20[last]), ("broken_support", support, support))
        for name, at_rally, now in levels:
            if at_rally is None or now is None or not np.isfinite(at_rally) or not np.isfinite(now):
                continue
            if high[j] >= at_rally and close[last] < now:
                return {"rally_bar": j - last, "level": name, "level_at_rally": rnd(at_rally),
                        "level_now": rnd(now), "rally_volume_ratio": rnd(volume[j] / avg)}
    return None


def check_down(m: Measures, rs: dict, flags: dict | None, p: DownParams) -> tuple[list[str], dict]:
    """Apply the rules to one name's measures. Returns (failed rules as text, extra metrics); no text is a pass."""
    reasons, extra = [], {}
    if not borrow_ok(p.borrow, flags):
        reasons.append(borrow_text(p.borrow, flags))
    if m.bars < p.min_bars:
        return [f"only {m.bars} daily bars; needs {p.min_bars}", *reasons], {}
    if p.below_sma200:
        if not m.close < m.sma50 < m.sma200:
            reasons.append(f"close {m.close:.2f} < SMA50 {m.sma50:.2f} < SMA200 {m.sma200:.2f} does not hold")
    elif not m.close < m.sma50:
        reasons.append(f"close {m.close:.2f} is not below SMA50 {m.sma50:.2f}")
    if not m.adx >= p.adx_min:
        reasons.append(f"ADX {m.adx:.1f} is under {p.adx_min:g}")

    if p.rs_rank_max is not None:
        rank, strength = rs.get("rs_rank"), rs.get("strength_vs_spy")
        ok = rank is not None and rank <= p.rs_rank_max
        if p.rs_or_lags_spy:
            ok = ok or (strength is not None and strength < 0)
        if not ok:
            spy = f" and strength vs SPY {_pct(strength)} is not below 0" if p.rs_or_lags_spy else ""
            reasons.append(f"RS rank {_num(rank)} is over {p.rs_rank_max:g}{spy}")

    if p.structure == FAILED_RALLY:
        rally = failed_rally(m, p)
        heavy = np.isfinite(m.volume_avg20) and m.volume >= p.breakdown_volume_min * m.volume_avg20
        extra["failed_rally"] = rally
        extra["breakdown_volume_ratio"] = rnd(m.volume / m.volume_avg20) if m.volume_avg20 else None
        if rally is None:
            reasons.append(f"no light-volume rally into SMA20 or broken support in the last {p.rally_sessions} "
                           "sessions that the as-of close fell back below")
        if not heavy:
            reasons.append(f"as-of volume {m.volume:,.0f} is under {p.breakdown_volume_min:g}x the 20-day average "
                           f"{m.volume_avg20:,.0f}")
    elif p.structure == BELOW_SWING_HIGH_OR_SMA20:
        high = m.last_swing(SWING_HIGH)
        if not ((high is not None and m.close < high.price) or m.close < m.sma20):
            shown = f"{high.price:.2f}" if high else "none"
            reasons.append(f"close {m.close:.2f} is not below the last swing high ({shown}) or SMA20 {m.sma20:.2f}")
    elif p.structure == BELOW_SMA20_OR_SMA50:
        if not (m.close < m.sma20 or m.close < m.sma50):
            reasons.append(f"close {m.close:.2f} is not below SMA20 {m.sma20:.2f} or SMA50 {m.sma50:.2f}")
    else:
        raise ValueError(f"unknown structure {p.structure!r}")

    if not in_band(m.rsi, p.rsi):
        reasons.append(f"RSI {m.rsi:.1f} is outside {p.rsi[0]:g}-{p.rsi[1]:g}")
    return reasons, extra


def build_basket(
    inputs: ScanInputs, params: DownParams, by_symbol: dict[str, pd.DataFrame], spy: pd.DataFrame,
    created_at: datetime | None = None,
) -> TickerBasket:
    """Apply the rules to every STRONG survivor and build the basket."""
    candidates = sorted(inputs.strong)
    rs = relative_strength(by_symbol, spy, inputs.universe)
    measures = {s: measure(by_symbol[s]) if s in by_symbol else None for s in candidates}

    members, rejected = [], []
    for s in candidates:
        m, flags = measures[s], inputs.borrow.get(s)
        if m is None:
            borrow = [] if borrow_ok(params.borrow, flags) else [borrow_text(params.borrow, flags)]
            rejected.append(Rejection(s, "; ".join(["no bars", *borrow])))
            continue
        reasons, extra = check_down(m, rs[s], flags, params)
        if reasons:
            rejected.append(Rejection(s, "; ".join(reasons)))
            continue
        strength = rs[s]["strength_vs_spy"]
        borrow = {"shortable": (flags or {}).get("shortable"), "easy_to_borrow": (flags or {}).get("easy_to_borrow")}
        members.append(BasketMember(s, round(-strength, 6) if strength is not None else 0.0,
                                    {**m.metrics(), **rs[s], **borrow, **extra}))
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
    inputs: ScanInputs, params: DownParams, *, bars_fetcher: Callable = fetch_daily_bars,
    cache_dir: str | Path | None = None, created_at: datetime | None = None,
) -> TickerBasket:
    """Fetch read-only daily bars for the candidates and SPY, then build the basket. A bars failure raises."""
    by_symbol, spy = fetch_bars(inputs, bars_fetcher=bars_fetcher, cache_dir=cache_dir)
    return build_basket(inputs, params, by_symbol, spy, created_at)


def _num(value) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _pct(value) -> str:
    return "n/a" if value is None else f"{100 * value:+.2f}%"
