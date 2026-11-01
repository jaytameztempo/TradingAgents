"""What the category scans share: candidates, financial groups, borrow flags, bars, RS and measurements.

Everything is reused from SCANBot. The candidates are the universe report's
liquidity survivors behind a financials report. The financial groups are that
report's STRONG and WEAK passes, plus NEUTRAL: names that had fundamentals,
failed one of the STRONG metric gates and are not WEAK survivors. shortable and
easy_to_borrow come from the asset snapshot behind the universe report, unless
another snapshot is given. Bars are the funnel's split-adjusted SIP daily bars,
dated on or before as_of. The indicators are RegimeBot's SMA and ADX, and the
swings module's ATR, RSI and ZigZag (5% reversal, confirmed on close).

RS rank is the percentile (0-100) of the 63-session return across every
candidate that has one. Strength vs SPY is that return minus SPY's, both
measured from SPY's session 63 sessions before its last bar. Missing SPY bars
fail the run closed. Nothing here places an order.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from extensions.market_data.alpaca_assets import load_snapshot
from extensions.market_data.alpaca_bars import MARKET_TZ, fetch_daily_bars
from extensions.regime.indicators import sma, wilder_adx
from extensions.regime.regime_bot import ADX_PERIOD, LOOKBACK_CALENDAR_DAYS
from extensions.scanbot.financials import REQUIRED, STRONG, WEAK, FinancialsReport
from extensions.scanbot.financials import load_report as load_financials
from extensions.scanbot.funnel import FEED, _fetch_in_chunks
from extensions.scanbot.funnel import load_report as load_universe
from extensions.scanbot.short_channel import borrow_flags
from extensions.scanbot.swings import (
    ATR_PERIOD,
    RSI_PERIOD,
    SWING_HIGH,
    SWING_LOW,
    SWING_REVERSAL_PCT,
    Swing,
    wilder_atr,
    wilder_rsi,
    zigzag_swings,
)
from extensions.scans.categories import (
    EASY_TO_BORROW,
    NEUTRAL,
    RS_SESSIONS,
    SHORTABLE_AND_ETB,
    SHORTABLE_OR_ETB,
    SMA20,
    SMA50,
    SMA50_SLOPE_SESSIONS,
    SWING_LOW as SWING_LOW_LEVEL,
    VOLUME_AVERAGE_SESSIONS,
    VOLUME_FAST_SESSIONS,
)

BENCHMARK = "SPY"
EDGE = 1e-9  # inclusive band edges, with room for float noise


class MissingBenchmark(RuntimeError):
    """SPY's bars do not cover the 63-session RS window, so strength vs SPY cannot be measured."""


# --- the inputs behind a financials report ---------------------------------------

@dataclass(frozen=True)
class ScanInputs:
    as_of: date
    universe: list[str]  # the universe report's liquidity survivors
    strong: frozenset[str]
    weak: frozenset[str]
    neutral: frozenset[str]
    borrow: dict[str, dict[str, bool | None]]  # symbol -> {shortable, easy_to_borrow}
    sources: dict  # report and snapshot paths, recorded in every basket
    warnings: list[str] = field(default_factory=list)

    def group(self, name: str) -> frozenset[str]:
        return {STRONG: self.strong, WEAK: self.weak, NEUTRAL: self.neutral}[name]


def neutral_names(financials: FinancialsReport) -> set[str]:
    """Names the STRONG pass removed at a metric gate (so they had fundamentals) that are not WEAK survivors."""
    metric_gates = {name for name, _ in REQUIRED}
    removed = {r.symbol for r in financials.gate_pass(STRONG).removed if r.gate in metric_gates}
    return removed - set(financials.gate_pass(WEAK).survivors)


def load_inputs(financials_path: str | Path, assets_snapshot: str | Path | None = None) -> ScanInputs:
    """Read the financials report, the universe report behind it and an asset snapshot. Raises on a bad file."""
    financials = load_financials(financials_path)
    universe = load_universe(financials.universe_report)
    snapshot = Path(assets_snapshot) if assets_snapshot is not None else Path(universe.assets_source)
    fetched_at, assets = load_snapshot(snapshot)
    as_of = financials.as_of

    warnings = [w for w in financials.warnings if "point in time" in w]
    point_in_time = fetched_at.astimezone(MARKET_TZ).date() <= as_of
    if not point_in_time:
        warnings.append(
            f"shortable and easy_to_borrow come from an asset snapshot fetched {fetched_at.isoformat()}, "
            "after as_of: they are not point in time"
        )
    return ScanInputs(
        as_of=as_of,
        universe=sorted(universe.survivors),
        strong=frozenset(financials.gate_pass(STRONG).survivors),
        weak=frozenset(financials.gate_pass(WEAK).survivors),
        neutral=frozenset(neutral_names(financials)),
        borrow=borrow_flags(assets),
        sources={
            "financials_report": str(financials_path),
            "financials_created_at": financials.created_at.isoformat(),
            "universe_report": str(financials.universe_report),
            "liquidity_preset": universe.preset,
            "assets_snapshot": str(snapshot),
            "assets_fetched_at": fetched_at.isoformat(),
            "borrow_point_in_time": point_in_time,
        },
        warnings=warnings,
    )


def borrow_ok(rule: str, flags: dict | None) -> bool:
    """Whether the asset snapshot's flags meet a borrow rule. A missing flag counts as false."""
    flags = flags or {}
    shortable, easy = flags.get("shortable") is True, flags.get("easy_to_borrow") is True
    if rule == SHORTABLE_AND_ETB:
        return shortable and easy
    if rule == EASY_TO_BORROW:
        return easy
    if rule == SHORTABLE_OR_ETB:
        return shortable or easy
    raise ValueError(f"unknown borrow rule {rule!r}")


def borrow_text(rule: str, flags: dict | None) -> str:
    flags = flags or {}
    return (f"borrow needs {rule.replace('_', ' ')}; snapshot has shortable {flags.get('shortable')!r}, "
            f"easy_to_borrow {flags.get('easy_to_borrow')!r}")


# --- bars and relative strength ---------------------------------------------------

def fetch_bars(
    inputs: ScanInputs, symbols=None, *, benchmark: bool = True, bars_fetcher: Callable = fetch_daily_bars,
    cache_dir: str | Path | None = None,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame | None]:
    """(symbol -> bars, SPY bars) on or before as_of, for ``symbols`` (default: every candidate) and, when
    ``benchmark`` is set, SPY, which must then cover the RS window. A bars failure raises."""
    symbols = set(inputs.universe if symbols is None else symbols)
    symbols = sorted(symbols | {BENCHMARK} if benchmark else symbols)
    start = inputs.as_of - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    bars = _fetch_in_chunks(bars_fetcher, symbols, start, inputs.as_of, cache_dir) if symbols else pd.DataFrame()
    if not bars.empty:
        bars = bars[bars["date"] <= pd.Timestamp(inputs.as_of)]
    by_symbol = {
        s: rows.dropna(subset=["high", "low", "close"]).sort_values("date").reset_index(drop=True)
        for s, rows in (bars.groupby("symbol") if not bars.empty else [])
    }
    spy = by_symbol.get(BENCHMARK)
    if not benchmark:
        return by_symbol, None
    if spy is None or len(spy) < RS_SESSIONS + 1:
        have = 0 if spy is None else len(spy)
        raise MissingBenchmark(f"{BENCHMARK} has {have} daily bars on or before {inputs.as_of}; "
                               f"RS needs {RS_SESSIONS + 1}")
    return by_symbol, spy


def relative_strength(by_symbol: dict[str, pd.DataFrame], spy: pd.DataFrame, symbols) -> dict[str, dict]:
    """symbol -> {return_63, strength_vs_spy, rs_rank}. Each is None for a name with no bar on or before
    SPY's start session."""
    start_date = spy["date"].iloc[-1 - RS_SESSIONS]
    spy_return = float(spy["close"].iloc[-1] / spy["close"].iloc[-1 - RS_SESSIONS] - 1)
    returns = {}
    for s in symbols:
        rows = by_symbol.get(s)
        if rows is None or rows.empty:
            continue
        before = rows[rows["date"] <= start_date]
        if not before.empty and before["close"].iloc[-1] > 0:
            returns[s] = float(rows["close"].iloc[-1] / before["close"].iloc[-1] - 1)
    ranks = (pd.Series(returns).rank(pct=True) * 100).to_dict() if returns else {}
    out = {}
    for s in symbols:
        r = returns.get(s)
        out[s] = {
            "return_63": rnd(r),
            "strength_vs_spy": rnd(r - spy_return) if r is not None else None,
            "rs_rank": rnd(ranks.get(s)),
        }
    return out


# --- one name ------------------------------------------------------------------------

@dataclass(frozen=True)
class Measures:
    bars: int
    last_bar_date: str
    close: float
    sma20: float
    sma50: float
    sma200: float
    sma50_prior: float  # SMA50 SMA50_SLOPE_SESSIONS bars before the as-of bar
    adx: float
    atr: float
    rsi: float
    volume: float  # the as-of bar
    volume_avg5: float  # the last 5 bars, as-of included
    volume_avg20: float  # the 20 bars before the as-of bar
    swings: list[Swing] = field(default_factory=list, compare=False)
    series: dict[str, np.ndarray] = field(default_factory=dict, compare=False, repr=False)

    def last_swing(self, kind: str) -> Swing | None:
        found = [s for s in self.swings if s.kind == kind]
        return max(found, key=lambda s: s.index) if found else None

    def level(self, name: str) -> float | None:
        """The price of a proximity level: SMA20, SMA50 or the last confirmed swing low."""
        if name == SMA20:
            return finite(self.sma20)
        if name == SMA50:
            return finite(self.sma50)
        if name == SWING_LOW_LEVEL:
            swing = self.last_swing(SWING_LOW)
            return swing.price if swing else None
        raise ValueError(f"unknown level {name!r}")

    def atr_distance(self, price: float | None) -> float | None:
        """|close - price| / ATR, or None when either is missing."""
        if price is None or not np.isfinite(self.atr) or self.atr <= 0:
            return None
        return abs(self.close - price) / self.atr

    def metrics(self) -> dict:
        ratio = self.volume / self.volume_avg20 if np.isfinite(self.volume_avg20) and self.volume_avg20 > 0 else None
        ratio5 = (self.volume_avg5 / self.volume_avg20
                  if np.isfinite(self.volume_avg20) and self.volume_avg20 > 0 else None)
        high, low = self.last_swing(SWING_HIGH), self.last_swing(SWING_LOW)
        return {
            "close": rnd(self.close),
            "sma_20": rnd(self.sma20),
            "sma_50": rnd(self.sma50),
            "sma_200": rnd(self.sma200),
            f"adx_{ADX_PERIOD}": rnd(self.adx),
            f"atr_{ATR_PERIOD}": rnd(self.atr),
            f"rsi_{RSI_PERIOD}": rnd(self.rsi),
            "volume_ratio_20": rnd(ratio),
            "volume_ratio_5_20": rnd(ratio5),
            "last_swing_high": rnd(high.price) if high else None,
            "last_swing_low": rnd(low.price) if low else None,
            "bars": self.bars,
            "last_bar_date": self.last_bar_date,
        }


def measure(bars: pd.DataFrame) -> Measures | None:
    """Measure one symbol's bars (oldest first, nothing after as_of). None when there are no bars."""
    bars = bars.dropna(subset=["high", "low", "close"]).sort_values("date").reset_index(drop=True)
    n = len(bars)
    if n == 0:
        return None
    high, low, close = bars["high"], bars["low"], bars["close"]
    volume = bars["volume"].astype(float) if "volume" in bars else pd.Series(np.full(n, np.nan))
    sma20_series = sma(close, 20)

    def last(series: pd.Series, back: int = 0) -> float:
        return float(series.iloc[-1 - back]) if len(series) > back else np.nan

    return Measures(
        bars=n,
        last_bar_date=bars["date"].iloc[-1].date().isoformat(),
        close=float(close.iloc[-1]),
        sma20=last(sma20_series),
        sma50=last(sma(close, 50)),
        sma200=last(sma(close, 200)),
        sma50_prior=last(sma(close, 50), SMA50_SLOPE_SESSIONS),
        adx=last(wilder_adx(high, low, close, ADX_PERIOD)),
        atr=last(wilder_atr(high, low, close)),
        rsi=last(wilder_rsi(close)),
        volume=float(volume.iloc[-1]),
        volume_avg5=float(volume.iloc[-VOLUME_FAST_SESSIONS:].mean()) if n >= VOLUME_FAST_SESSIONS else np.nan,
        volume_avg20=(float(volume.iloc[-1 - VOLUME_AVERAGE_SESSIONS:-1].mean())
                      if n > VOLUME_AVERAGE_SESSIONS else np.nan),
        swings=zigzag_swings(high, low, close),
        series={
            "high": high.to_numpy(dtype=float),
            "low": low.to_numpy(dtype=float),
            "close": close.to_numpy(dtype=float),
            "volume": volume.to_numpy(dtype=float),
            "sma20": sma20_series.to_numpy(dtype=float),
        },
    )


def in_band(value: float | None, band: tuple[float, float]) -> bool:
    return value is not None and np.isfinite(value) and band[0] - EDGE <= value <= band[1] + EDGE


def finite(value) -> float | None:
    return float(value) if value is not None and np.isfinite(value) else None


def rnd(value) -> float | None:
    return round(float(value), 4) if value is not None and np.isfinite(value) else None


# --- the basket's parameters -----------------------------------------------------

def stale_warning(measures: dict[str, Measures]) -> list[str]:
    dates = {s: m.last_bar_date for s, m in measures.items() if m is not None}
    if not dates:
        return []
    newest = max(dates.values())
    stale = sorted(s for s, d in dates.items() if d < newest)
    return [f"{len(stale)} names have no bar on {newest}: {', '.join(stale)}"] if stale else []


def basket_parameters(
    inputs: ScanInputs, *, regime: str, side: str, params, financial_gate: str | None,
    rs_population: int, score: str, warnings: list[str],
) -> dict:
    """The parameters block every category basket carries, so a later reader can explain each name."""
    return {
        "category": params.category,
        "regime": regime,
        "side": side,
        "rules": params.to_dict(),
        "financial_gate": financial_gate,
        "feed": FEED,
        "adjustment": "split",
        "lookback_calendar_days": LOOKBACK_CALENDAR_DAYS,
        "adx_period": ADX_PERIOD,
        "atr_period": ATR_PERIOD,
        "rsi_period": RSI_PERIOD,
        "swing_method": "zigzag_high_low_confirmed_on_close",
        "swing_reversal_pct": SWING_REVERSAL_PCT,
        "rs_sessions": RS_SESSIONS,
        "rs_benchmark": BENCHMARK,
        "rs_rank_population": rs_population,
        "volume_average": f"{VOLUME_AVERAGE_SESSIONS} sessions before the as-of bar",
        "score": score,
        "inputs": dict(inputs.sources),
        "warnings": [*inputs.warnings, *warnings],
        "places_orders": False,
    }
