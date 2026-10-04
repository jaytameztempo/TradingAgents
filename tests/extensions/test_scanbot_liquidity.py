"""SCANBot's liquidity gates measure the last 20 sessions and the sampled spread, and explain every removal.

Bars and quotes here are made up; nothing calls Alpaca.
"""

from datetime import date

import pandas as pd
import pytest

from extensions.scanbot.liquidity import (
    combine_windows,
    gate_dollar_volume,
    gate_history,
    gate_share_volume,
    gate_spread,
    gate_zero_volume,
    liquidity_metrics,
    recent_sessions,
    window_spreads,
)

AS_OF = date(2026, 9, 30)
DAYS = list(pd.bdate_range(end=pd.Timestamp(AS_OF), periods=30))


def _bars(symbol, days=DAYS, volume=1_000_000.0, close=50.0):
    return pd.DataFrame({"symbol": symbol, "date": list(days), "close": close, "volume": volume})


SESSIONS = recent_sessions(_bars("SPY"), AS_OF)


@pytest.mark.unit
def test_recent_sessions_are_the_last_20_on_or_before_the_date():
    later = pd.DataFrame({"symbol": "SPY", "date": [pd.Timestamp("2026-10-01")]})
    sessions = recent_sessions(pd.concat([_bars("SPY"), later]), AS_OF)
    assert len(sessions) == 20
    assert sessions[-1] == pd.Timestamp(AS_OF) and sessions[0] == DAYS[10]


@pytest.mark.unit
def test_too_few_calendar_sessions_raise():
    with pytest.raises(ValueError, match="only 5 sessions"):
        recent_sessions(_bars("SPY", DAYS[-5:]), AS_OF)


@pytest.mark.unit
def test_history_needs_a_bar_on_or_before_the_window_start():
    bars = pd.concat([_bars("OLD"), _bars("EDGE", DAYS[10:]), _bars("NEW", DAYS[11:])])
    kept, removed = gate_history(["OLD", "EDGE", "NEW", "NONE"], bars, SESSIONS)
    assert kept == ["OLD", "EDGE"]
    reasons = {r.symbol: r.reason for r in removed}
    assert reasons["NEW"].startswith(f"first bar {DAYS[11].date()} is after the window start {DAYS[10].date()}")
    assert reasons["NONE"] == "no daily bars in the lookback"


@pytest.mark.unit
def test_a_missing_or_zero_volume_session_fails():
    gap = _bars("GAP").drop(index=25)
    zero = _bars("ZERO")
    zero.loc[27, "volume"] = 0
    early = _bars("EARLY")
    early.loc[2, "volume"] = 0  # before the 20-session window
    bars = pd.concat([gap, zero, early, _bars("FULL")])

    kept, removed = gate_zero_volume(["GAP", "ZERO", "EARLY", "FULL"], bars, SESSIONS)

    assert kept == ["EARLY", "FULL"]
    reasons = {r.symbol: r.reason for r in removed}
    assert reasons["GAP"] == f"1 of the last 20 sessions had no trades (first {DAYS[25].date()})"
    assert reasons["ZERO"] == f"1 of the last 20 sessions had no trades (first {DAYS[27].date()})"


@pytest.mark.unit
def test_averages_are_over_the_20_sessions_only():
    bars = _bars("X", volume=1_000.0, close=10.0)
    bars.loc[0, "volume"] = 9_999_999.0  # outside the window
    metrics = liquidity_metrics(bars, SESSIONS)
    assert metrics.loc["X", "avg_share_volume_20"] == 1_000.0
    assert metrics.loc["X", "avg_dollar_volume_20"] == 10_000.0


@pytest.mark.unit
def test_share_and_dollar_floors_are_inclusive():
    bars = pd.concat([
        _bars("AT", volume=500_000.0, close=40.0),     # exactly 500k shares, $20M
        _bars("BELOW", volume=499_999.0, close=100.0),  # one share short
        _bars("CHEAP", volume=600_000.0, close=10.0),   # $6M
    ])
    metrics = liquidity_metrics(bars, SESSIONS)

    kept, removed = gate_share_volume(["AT", "BELOW", "CHEAP"], metrics, 500_000)
    assert kept == ["AT", "CHEAP"]
    assert removed[0].reason == "avg_share_volume_20 499,999 is below 500,000"

    kept, removed = gate_dollar_volume(["AT", "CHEAP"], metrics, 20_000_000)
    assert kept == ["AT"]
    assert removed[0].reason == "avg_dollar_volume_20 $6,000,000 is below $20,000,000"


def _quotes(rows):
    return pd.DataFrame(rows, columns=["symbol", "bid_price", "ask_price"]).assign(timestamp=pd.Timestamp("2026-09-30"))


@pytest.mark.unit
def test_window_spread_is_the_median_percent_of_mid_and_ignores_bad_quotes():
    quotes = _quotes([
        ("X", 99.95, 100.05),  # 0.10%
        ("X", 99.90, 100.10),  # 0.20%
        ("X", 99.85, 100.15),  # 0.30%
        ("X", 0.0, 100.0),     # missing bid
        ("X", 100.2, 100.1),   # crossed
    ])
    assert window_spreads(quotes)["X"] == pytest.approx(0.2)


@pytest.mark.unit
def test_a_window_with_no_valid_quotes_is_empty():
    assert window_spreads(_quotes([("X", 0.0, 0.0)])).empty


@pytest.mark.unit
def test_windows_combine_by_median_and_count():
    combined = combine_windows([pd.Series({"X": 0.1, "Y": 0.5}), pd.Series({"X": 0.3}), pd.Series({"X": 0.2})])
    assert combined == {"X": (pytest.approx(0.2), 3), "Y": (0.5, 1)}


@pytest.mark.unit
def test_spread_gate_is_inclusive_and_explains_removals():
    spreads = {"AT": (0.15, 15), "WIDE": (0.4, 15)}
    kept, removed = gate_spread(["AT", "WIDE", "NOQ"], spreads, 0.15)
    assert kept == ["AT"]
    reasons = {r.symbol: r.reason for r in removed}
    assert reasons["WIDE"] == "median spread 0.400% over 15 windows is above 0.15%"
    assert reasons["NOQ"] == "no quotes in the sampled windows"
