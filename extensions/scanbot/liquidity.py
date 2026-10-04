"""SCANBot stage 2: liquidity, from daily bars and sampled quotes.

Gates, in funnel order, each returning (kept, removed):

6. history: the name has a bar on or before the first of the last 20 sessions
7. zero_volume: every one of the last 20 sessions has a bar with volume above 0
8. share_volume: 20-session average share volume at or above the floor
9. dollar_volume: SMA(close * volume, 20) at or above the floor
10. spread: median sampled bid-ask spread over the last 5 sessions at or below the cap

Sessions come from a calendar symbol's bars (SPY), so a missing bar for a name
on a session SPY traded is a session with no trades. Only bars dated on or
before the as-of date are used.
"""

from __future__ import annotations

import pandas as pd

from extensions.scanbot.universe import GateResult, Removal

SESSION_WINDOW = 20
SPREAD_SESSIONS = 5


def recent_sessions(calendar_bars: pd.DataFrame, as_of, n: int = SESSION_WINDOW) -> list[pd.Timestamp]:
    """The last n trading dates on or before as_of, oldest first."""
    dates = sorted(set(calendar_bars.loc[calendar_bars["date"] <= pd.Timestamp(as_of), "date"]))
    if len(dates) < n:
        raise ValueError(f"the calendar symbol has only {len(dates)} sessions on or before {as_of}; needs {n}")
    return dates[-n:]


def _window(bars: pd.DataFrame, sessions: list[pd.Timestamp]) -> pd.DataFrame:
    return bars[bars["date"].isin(sessions)]


def _day(ts: pd.Timestamp) -> str:
    return ts.date().isoformat()


def gate_history(symbols: list[str], bars: pd.DataFrame, sessions: list[pd.Timestamp]) -> GateResult:
    first = sessions[0]
    earliest = bars.groupby("symbol")["date"].min()
    kept, removed = [], []
    for symbol in symbols:
        start = earliest.get(symbol)
        if start is None:
            removed.append(Removal(symbol, "history", "no daily bars in the lookback"))
        elif start > first:
            removed.append(
                Removal(symbol, "history", f"first bar {_day(start)} is after the window start {_day(first)}")
            )
        else:
            kept.append(symbol)
    return kept, removed


def gate_zero_volume(symbols: list[str], bars: pd.DataFrame, sessions: list[pd.Timestamp]) -> GateResult:
    window = _window(bars, sessions)
    traded = window[window["volume"] > 0].groupby("symbol")["date"].agg(set)
    kept, removed = [], []
    for symbol in symbols:
        days = traded.get(symbol, set())
        quiet = [s for s in sessions if s not in days]
        if quiet:
            removed.append(Removal(
                symbol, "zero_volume",
                f"{len(quiet)} of the last {len(sessions)} sessions had no trades (first {_day(quiet[0])})",
            ))
        else:
            kept.append(symbol)
    return kept, removed


def liquidity_metrics(bars: pd.DataFrame, sessions: list[pd.Timestamp]) -> pd.DataFrame:
    """Per symbol: avg_share_volume_20 and avg_dollar_volume_20 over the sessions."""
    window = _window(bars, sessions).assign(dollar_volume=lambda f: f["close"] * f["volume"])
    grouped = window.groupby("symbol")
    return pd.DataFrame({
        "avg_share_volume_20": grouped["volume"].sum() / len(sessions),
        "avg_dollar_volume_20": grouped["dollar_volume"].sum() / len(sessions),
    })


def _floor_gate(name: str, column: str, unit: str):
    def gate(symbols: list[str], metrics: pd.DataFrame, floor: float) -> GateResult:
        kept, removed = [], []
        for symbol in symbols:
            value = float(metrics[column].get(symbol, 0.0))
            if value >= floor:
                kept.append(symbol)
            else:
                removed.append(Removal(symbol, name, f"{column} {unit}{value:,.0f} is below {unit}{floor:,.0f}"))
        return kept, removed

    gate.__name__ = f"gate_{name}"
    return gate


gate_share_volume = _floor_gate("share_volume", "avg_share_volume_20", "")
gate_dollar_volume = _floor_gate("dollar_volume", "avg_dollar_volume_20", "$")


def window_spreads(quotes: pd.DataFrame) -> pd.Series:
    """Median spread as a percent of mid, per symbol, over one sampling window.

    Quotes with a missing side, a zero price, or a crossed book are ignored.
    """
    valid = quotes[(quotes["bid_price"] > 0) & (quotes["ask_price"] >= quotes["bid_price"])]
    if valid.empty:
        return pd.Series(dtype=float)
    mid = (valid["ask_price"] + valid["bid_price"]) / 2
    spread_pct = (valid["ask_price"] - valid["bid_price"]) / mid * 100
    return spread_pct.groupby(valid["symbol"]).median()


def combine_windows(per_window: list[pd.Series]) -> dict[str, tuple[float, int]]:
    """Per symbol: (median of its window medians, number of windows it was quoted in)."""
    samples: dict[str, list[float]] = {}
    for series in per_window:
        for symbol, value in series.items():
            samples.setdefault(symbol, []).append(float(value))
    return {symbol: (float(pd.Series(values).median()), len(values)) for symbol, values in samples.items()}


def gate_spread(symbols: list[str], spreads: dict[str, tuple[float, int]], max_pct: float) -> GateResult:
    kept, removed = [], []
    for symbol in symbols:
        if symbol not in spreads:
            removed.append(Removal(symbol, "spread", "no quotes in the sampled windows"))
            continue
        median, windows = spreads[symbol]
        if median <= max_pct:
            kept.append(symbol)
        else:
            removed.append(Removal(
                symbol, "spread", f"median spread {median:.3f}% over {windows} windows is above {max_pct:.2f}%"
            ))
    return kept, removed
