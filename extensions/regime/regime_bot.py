"""RegimeBot: label one symbol UP, DOWN or SIDE as of a date.

Rules, in order:

- SIDE if there are fewer than 200 bars, the 50-day and 200-day averages are
  tangled, or ADX is under 20.
- UP if the close is above the 50-day, the 50-day is above the 200-day, and ADX
  is at least 25.
- DOWN if the close is below the 50-day, the 50-day is below the 200-day, and
  ADX is at least 25.
- SIDE for anything else.

Tangled means the 50-day is within 1% of the 200-day, or the two crossed in the
last 20 trading days. When the rules do not confirm a trend, the answer is SIDE.

Only bars dated on or before the label date are used. RegimeBot reads bars and
writes a label. It places no orders.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from extensions.market_data.alpaca_bars import (
    fetch_daily_bars,
    validate_date_range,
    validate_ticker,
)
from extensions.regime.indicators import sma, wilder_adx

UP, DOWN, SIDE = "UP", "DOWN", "SIDE"
LABELS = (UP, DOWN, SIDE)

DEFAULT_SYMBOL = "SPY"
FAST_WINDOW = 50
SLOW_WINDOW = 200
MIN_BARS = 200
ADX_PERIOD = 14
ADX_SIDE_BELOW = 20.0
ADX_TREND_MIN = 25.0
TANGLE_SPREAD_PCT = 1.0
TANGLE_CROSS_DAYS = 20
# About 275 trading days: enough for a 200-day average with room for holidays.
LOOKBACK_CALENDAR_DAYS = 400

PARAMETERS = {
    "fast_window": FAST_WINDOW,
    "slow_window": SLOW_WINDOW,
    "min_bars": MIN_BARS,
    "adx_period": ADX_PERIOD,
    "adx_side_below": ADX_SIDE_BELOW,
    "adx_trend_min": ADX_TREND_MIN,
    "tangle_spread_pct": TANGLE_SPREAD_PCT,
    "tangle_cross_days": TANGLE_CROSS_DAYS,
    "lookback_calendar_days": LOOKBACK_CALENDAR_DAYS,
}

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RegimeLabel:
    symbol: str
    as_of: date
    created_at: datetime
    label: str
    reasons: list[str]
    metrics: dict
    parameters: dict
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> RegimeLabel:
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported regime schema_version: {data.get('schema_version')!r}")
        if data.get("label") not in LABELS:
            raise ValueError(f"unknown regime label: {data.get('label')!r}")
        return cls(
            symbol=data["symbol"],
            as_of=date.fromisoformat(data["as_of"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            label=data["label"],
            reasons=list(data["reasons"]),
            metrics=dict(data["metrics"]),
            parameters=dict(data["parameters"]),
        )


def tangle_check(sma_fast: pd.Series, sma_slow: pd.Series) -> tuple[bool, float, bool]:
    """Return (tangled, spread_pct, crossed_recently) from the latest averages.

    spread_pct is how far the fast average sits above (+) or below (-) the slow
    one. A cross is any change of side between consecutive days in the last
    TANGLE_CROSS_DAYS trading days, using the days where both averages exist.
    """
    both = pd.DataFrame({"fast": sma_fast, "slow": sma_slow}).dropna()
    fast, slow = both["fast"].iloc[-1], both["slow"].iloc[-1]
    spread_pct = float((fast / slow - 1) * 100)
    sides = np.sign((both["fast"] - both["slow"]).iloc[-(TANGLE_CROSS_DAYS + 1):].to_numpy())
    crossed = bool((sides[1:] != sides[:-1]).any())
    return abs(spread_pct) < TANGLE_SPREAD_PCT or crossed, spread_pct, crossed


def decide(*, close: float, sma_fast: float, sma_slow: float, adx: float, tangled: bool) -> tuple[str, list[str]]:
    """Apply the label rules to the latest numbers, for a symbol with enough bars."""
    side_reasons = []
    if tangled:
        side_reasons.append(
            f"{FAST_WINDOW}-day and {SLOW_WINDOW}-day averages are tangled "
            f"(within {TANGLE_SPREAD_PCT:g}% or crossed in the last {TANGLE_CROSS_DAYS} trading days)"
        )
    if adx < ADX_SIDE_BELOW:
        side_reasons.append(f"ADX {adx:.1f} is under {ADX_SIDE_BELOW:g}")
    if side_reasons:
        return SIDE, side_reasons

    aligned_up = close > sma_fast > sma_slow
    aligned_down = close < sma_fast < sma_slow
    trending = adx >= ADX_TREND_MIN
    numbers = f"close {close:.2f}, {FAST_WINDOW}-day {sma_fast:.2f}, {SLOW_WINDOW}-day {sma_slow:.2f}"
    if aligned_up and trending:
        return UP, [f"{numbers}: aligned up", f"ADX {adx:.1f} is at least {ADX_TREND_MIN:g}"]
    if aligned_down and trending:
        return DOWN, [f"{numbers}: aligned down", f"ADX {adx:.1f} is at least {ADX_TREND_MIN:g}"]

    reasons = []
    if not (aligned_up or aligned_down):
        reasons.append(f"{numbers}: neither aligned up nor aligned down")
    if not trending:
        reasons.append(f"ADX {adx:.1f} is between {ADX_SIDE_BELOW:g} and {ADX_TREND_MIN:g}, not a confirmed trend")
    return SIDE, reasons


def classify(bars: pd.DataFrame) -> tuple[str, list[str], dict]:
    """Label one symbol's bars (oldest first). Returns (label, reasons, metrics)."""
    bars = bars.dropna(subset=["high", "low", "close"]).sort_values("date").reset_index(drop=True)
    count = len(bars)
    if count < MIN_BARS:
        return SIDE, [f"only {count} daily bars; needs {MIN_BARS}"], {"bars": count}

    fast_series, slow_series = sma(bars["close"], FAST_WINDOW), sma(bars["close"], SLOW_WINDOW)
    adx = float(wilder_adx(bars["high"], bars["low"], bars["close"], ADX_PERIOD).iloc[-1])
    close, fast, slow = float(bars["close"].iloc[-1]), float(fast_series.iloc[-1]), float(slow_series.iloc[-1])
    tangled, spread_pct, crossed = tangle_check(fast_series, slow_series)

    label, reasons = decide(close=close, sma_fast=fast, sma_slow=slow, adx=adx, tangled=tangled)
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
    return label, reasons, metrics


def label_symbol(
    bars: pd.DataFrame, symbol: str, as_of: date, created_at: datetime | None = None
) -> RegimeLabel:
    """Label a symbol from a bars frame (as fetch_daily_bars returns it), ignoring bars after as_of."""
    symbol = validate_ticker(symbol)
    rows = bars[(bars["symbol"] == symbol) & (bars["date"] <= pd.Timestamp(as_of))]
    label, reasons, metrics = classify(rows)
    return RegimeLabel(
        symbol=symbol,
        as_of=as_of,
        created_at=created_at or datetime.now(UTC),
        label=label,
        reasons=reasons,
        metrics=metrics,
        parameters=dict(PARAMETERS),
    )


def run_regime_bot(
    as_of: date | str, symbol: str = DEFAULT_SYMBOL, *, client=None, cache_dir=None
) -> RegimeLabel:
    """Fetch read-only daily bars ending on the label date and label the symbol."""
    symbol = validate_ticker(symbol)
    _, as_of = validate_date_range(as_of, as_of)
    start = as_of - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    bars = fetch_daily_bars(symbol, start, as_of, client=client, cache_dir=cache_dir)
    return label_symbol(bars, symbol, as_of)


def default_regime_dir() -> Path:
    """~/.tradingagents/regimes, beside TradingAgents' own results and out of git."""
    return Path.home() / ".tradingagents" / "regimes"


def save_label(label: RegimeLabel, out_dir: str | Path | None = None) -> Path:
    """Write the label as a new file and return its path. An existing label is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_regime_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"regime_{label.symbol}_{label.as_of}_{label.created_at:%Y%m%dT%H%M%SZ}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(label.to_dict(), fh, indent=2)
        fh.write("\n")
    return path


def load_label(path: str | Path) -> RegimeLabel:
    return RegimeLabel.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
