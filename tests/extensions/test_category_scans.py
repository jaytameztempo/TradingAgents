"""SCANUP, SCANDOWN and SCANSIDE at Category 1, 2 and 3.

Each threshold is checked at its edge for every category that uses it. The scans
reuse the universe, financials, borrow flags and indicators, write TickerBaskets
that are never overwritten, ignore bars after as_of, and are named apart from
the router's and the UI's existing scans. No network.
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from extensions.market_data import alpaca_assets
from extensions.scanbot.financials import STRONG, WEAK, FinancialsReport, GatePass
from extensions.scanbot.financials import save_report as save_financials
from extensions.scanbot.funnel import FunnelReport
from extensions.scanbot.funnel import save_report as save_universe
from extensions.scanbot.swings import SWING_HIGH, SWING_LOW, Swing
from extensions.scanbot.universe import Removal
from extensions.scans import scan_down_category as down
from extensions.scans import scan_side_category as side
from extensions.scans import scan_up_category as up
from extensions.scans.basket import TickerBasket, basket_filename, load_basket, save_basket
from extensions.scans.categories import (
    DOWN,
    EASY_TO_BORROW,
    SHORTABLE_AND_ETB,
    SHORTABLE_OR_ETB,
    SIDE,
    UP,
    params_for,
)
from extensions.scans.category_inputs import (
    BENCHMARK,
    Measures,
    MissingBenchmark,
    ScanInputs,
    borrow_ok,
    fetch_bars,
    load_inputs,
    neutral_names,
    relative_strength,
)
from extensions.scripts import run_category_scan as cli

pytestmark = pytest.mark.unit

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 10, 7, 12, 5, tzinfo=UTC)
RS_OK = {"rs_rank": 90.0, "strength_vs_spy": 0.1}
BORROW_ALL = {"shortable": True, "easy_to_borrow": True}


# --- builders ------------------------------------------------------------------

def _m(**overrides) -> Measures:
    """Measures that pass SCANUP Category 1: close 100 above a rising SMA50 above SMA200, ADX 30,
    a 10% pullback from a 111.11 swing high, RSI 50, at SMA20, volume drying up."""
    values = dict(
        bars=260, last_bar_date="2026-09-30", close=100.0, sma20=100.0, sma50=95.0, sma200=80.0,
        sma50_prior=90.0, adx=30.0, atr=2.0, rsi=50.0, volume=800_000.0, volume_avg5=800_000.0,
        volume_avg20=1_000_000.0,
        swings=[Swing(SWING_LOW, 200, 90.0, 205), Swing(SWING_HIGH, 240, 100 / 0.9, 245)],
    )
    values.update(overrides)
    return Measures(**values)


def _down_m(**overrides) -> Measures:
    """Measures for SCANDOWN Cat 2 and 3: close 100 below a falling SMA50 below SMA200, ADX 30, RSI 50."""
    values = dict(close=100.0, sma20=104.0, sma50=105.0, sma200=120.0, sma50_prior=110.0, rsi=50.0,
                  swings=[Swing(SWING_HIGH, 200, 115.0, 205), Swing(SWING_LOW, 240, 95.0, 245)])
    values.update(overrides)
    return _m(**values)


def _channel(**overrides) -> dict:
    values = dict(support=95.0, resistance=108.0, support_touches=2, resistance_touches=2, height_pct=13.0,
                  distance_long_atr=0.3, distance_short_atr=0.3)
    values.update(overrides)
    return values


def _frame(symbol, closes, volume=1_000_000, end=AS_OF, spread=0.5):
    closes = np.array(closes, dtype=float)
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    volume = np.full(len(closes), volume, dtype=float) if np.isscalar(volume) else np.array(volume, dtype=float)
    return pd.DataFrame({"symbol": symbol, "date": dates, "open": closes, "high": closes + spread,
                         "low": closes - spread, "close": closes, "volume": volume})


def _saw(n, up_bars=14, down_bars=6, up_step=1.0, down_step=1.2, start=50.0):
    """An uptrend of 14 rising bars then 6 falling ones; the last bar ends a 6-bar pullback."""
    out, price = [], start
    while len(out) < n:
        for _ in range(up_bars):
            price += up_step
            out.append(price)
        for _ in range(down_bars):
            price -= down_step
            out.append(price)
    return out[:n]


def _triangle(n, lo=100.0, hi=112.0, half=10, end_phase=0):
    """A clean range; the last bar sits ``end_phase`` bars after a trough."""
    step = (hi - lo) / half
    out = []
    for i in range(n):
        k = (i - (n - 1) + end_phase) % (2 * half)
        out.append(lo + step * (k if k <= half else 2 * half - k))
    return out


SPY = _frame(BENCHMARK, [100 + 0.05 * i for i in range(260)])


class FakeBars:
    def __init__(self, frames):
        self.frames = frames
        self.requested = []

    def __call__(self, symbols, start, end, *, feed, cache_dir=None):
        self.requested.append(list(symbols))
        frames = [self.frames[s] for s in symbols if s in self.frames]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            {"symbol": [], "date": pd.to_datetime([]), "high": [], "low": [], "close": []})


def _inputs(universe, strong=(), weak=(), neutral=(), borrow=None) -> ScanInputs:
    return ScanInputs(
        as_of=AS_OF, universe=sorted(universe), strong=frozenset(strong), weak=frozenset(weak),
        neutral=frozenset(neutral), borrow=borrow or {},
        sources={"financials_report": "financials.json", "assets_snapshot": "assets.json",
                 "assets_fetched_at": "2026-09-30T22:00:00+00:00"},
    )


# --- the tables --------------------------------------------------------------------

def test_params_for_returns_each_table_and_refuses_anything_else():
    assert params_for("up", 1) is UP[1] and params_for("down", 2) is DOWN[2] and params_for("side", 3) is SIDE[3]
    with pytest.raises(ValueError):
        params_for("up", 4)
    with pytest.raises(ValueError):
        params_for("sideways", 1)


def test_categories_loosen_from_1_to_3():
    assert [UP[c].adx_min for c in (1, 2, 3)] == [25.0, 20.0, 15.0]
    assert [DOWN[c].adx_min for c in (1, 2, 3)] == [25.0, 20.0, 15.0]
    assert [SIDE[c].adx_max for c in (1, 2, 3)] == [20.0, 25.0, 30.0]
    assert [SIDE[c].height_pct for c in (1, 2, 3)] == [(8.0, 22.0), (6.0, 25.0), (5.0, 30.0)]
    assert [DOWN[c].borrow for c in (1, 2, 3)] == [SHORTABLE_AND_ETB, EASY_TO_BORROW, SHORTABLE_OR_ETB]
    assert [SIDE[c].short_borrow for c in (1, 2, 3)] == [EASY_TO_BORROW, EASY_TO_BORROW, SHORTABLE_OR_ETB]


# --- SCANUP ---------------------------------------------------------------------------

def _up(m=None, rs=RS_OK, category=1):
    return up.check_up(m or _m(), rs, UP[category])[0]


def test_up_reference_measures_pass_every_category():
    assert _up() == [] and _up(category=2) == [] and _up(category=3) == []


def test_up_bars_floor_is_200_except_cat_3():
    assert _up(_m(bars=199)) == ["only 199 daily bars; needs 200"]
    assert _up(_m(bars=199), category=2) == ["only 199 daily bars; needs 200"]
    assert _up(_m(bars=64), category=3) == []


def test_up_moving_average_stack():
    assert _up(_m(sma200=96.0)) and _up(_m(sma200=96.0), category=2)
    assert _up(_m(sma200=96.0), category=3) == []  # Cat 3 needs only close > SMA50
    assert _up(_m(sma50=100.0), category=3)  # strictly above
    assert _up(_m(sma50_prior=95.0)) and _up(_m(sma50_prior=95.0), category=2) == []  # rising on Cat 1 only


@pytest.mark.parametrize("category, floor", [(1, 25.0), (2, 20.0), (3, 15.0)])
def test_up_adx_floor_is_inclusive(category, floor):
    assert _up(_m(adx=floor), category=category) == []
    assert _up(_m(adx=floor - 0.01), category=category) == [f"ADX {floor - 0.01:.1f} is under {floor:g}"]


def test_up_rs_cat_1_passes_on_rank_80_or_beating_spy():
    assert _up(rs={"rs_rank": 80.0, "strength_vs_spy": -0.1}) == []
    assert _up(rs={"rs_rank": 10.0, "strength_vs_spy": 0.001}) == []
    assert _up(rs={"rs_rank": 79.9, "strength_vs_spy": 0.0})
    assert _up(rs={"rs_rank": None, "strength_vs_spy": None})


def test_up_rs_cat_2_needs_rank_60_and_cat_3_has_no_filter():
    assert _up(rs={"rs_rank": 60.0, "strength_vs_spy": -0.1}, category=2) == []
    assert _up(rs={"rs_rank": 59.9, "strength_vs_spy": 0.5}, category=2)  # beating SPY does not help on Cat 2
    assert _up(rs={"rs_rank": None, "strength_vs_spy": None}, category=3) == []


@pytest.mark.parametrize("category, band", [(1, (5.0, 15.0)), (2, (3.0, 20.0))])
def test_up_pullback_band_is_inclusive(category, band):
    def at(pct):  # SMA20 stays at the close, so Cat 1's proximity rule holds
        return _m(swings=[Swing(SWING_HIGH, 240, 100 / (1 - pct / 100), 245)])
    assert _up(at(band[0]), category=category) == [] and _up(at(band[1]), category=category) == []
    for pct in (band[0] - 0.01, band[1] + 0.01):
        assert any("pullback" in r for r in _up(at(pct), category=category))


def test_up_pullback_needs_a_confirmed_swing_high_on_cat_1_and_2_only():
    m = _m(swings=[Swing(SWING_LOW, 240, 99.0, 245)])
    assert "no confirmed ZigZag swing high to measure the pullback from" in _up(m)
    assert "no confirmed ZigZag swing high to measure the pullback from" in _up(m, category=2)
    assert _up(m, category=3) == []


def test_up_cat_1_needs_declining_volume():
    assert any("5-session average volume" in r for r in _up(_m(volume_avg5=1_000_000.0)))
    assert _up(_m(volume_avg5=1_000_000.0), category=2) == []


@pytest.mark.parametrize("category, band", [(1, (40.0, 55.0)), (2, (35.0, 60.0)), (3, (30.0, 70.0))])
def test_up_rsi_band_is_inclusive(category, band):
    assert _up(_m(rsi=band[0]), category=category) == [] and _up(_m(rsi=band[1]), category=category) == []
    assert _up(_m(rsi=band[0] - 0.01), category=category) and _up(_m(rsi=band[1] + 0.01), category=category)


def test_up_cat_1_proximity_is_0_to_1_atr_of_sma20_or_the_last_swing_low():
    assert _up(_m(sma20=100.0)) == []  # right on SMA20 passes
    assert _up(_m(sma20=98.0)) == []  # 1.0 ATR
    assert any("within 1 ATR" in r for r in _up(_m(sma20=97.9)))
    low_near = [Swing(SWING_LOW, 200, 99.0, 205), Swing(SWING_HIGH, 240, 100 / 0.9, 245)]
    assert _up(_m(sma20=90.0, swings=low_near)) == []  # the swing low is 0.5 ATR away
    assert _up(_m(sma20=90.0), category=2) == []  # Cat 2 has no proximity rule


def test_up_cat_3_proximity_is_2_atr_of_sma20_or_sma50():
    assert _up(_m(sma20=96.0), category=3) == []
    assert _up(_m(sma20=90.0, sma50=96.0), category=3) == []
    assert any("within 2 ATR" in r for r in _up(_m(sma20=95.9, sma50=95.9), category=3))


# --- SCANDOWN -------------------------------------------------------------------------

def _down(m=None, rs=None, flags=BORROW_ALL, category=2):
    rs = rs if rs is not None else {"rs_rank": 10.0, "strength_vs_spy": -0.1}
    return down.check_down(m or _down_m(), rs, flags, DOWN[category])[0]


def test_down_reference_measures_pass_cat_2_and_3():
    assert _down() == [] and _down(category=3) == []


def test_down_moving_average_stack():
    assert _down(_down_m(sma200=104.0))
    assert _down(_down_m(sma200=104.0), category=3) == []
    assert _down(_down_m(sma50=100.0), category=3)


@pytest.mark.parametrize("category, floor", [(2, 20.0), (3, 15.0)])
def test_down_adx_floor_is_inclusive(category, floor):
    assert _down(_down_m(adx=floor), category=category) == []
    assert _down(_down_m(adx=floor - 0.01), category=category)


def test_down_rs_ceilings():
    assert _down(rs={"rs_rank": 40.0, "strength_vs_spy": 0.2}) == []
    assert _down(rs={"rs_rank": 40.1, "strength_vs_spy": -0.2})  # lagging SPY does not help on Cat 2
    assert _down(rs={"rs_rank": None, "strength_vs_spy": None}, category=3) == []
    rules = DOWN[1]
    m = _rally()
    for rs, ok in (({"rs_rank": 20.0, "strength_vs_spy": 0.1}, True), ({"rs_rank": 90.0, "strength_vs_spy": -0.01}, True),
                   ({"rs_rank": 20.1, "strength_vs_spy": 0.0}, False)):
        assert any("RS rank" in r for r in down.check_down(m, rs, BORROW_ALL, rules)[0]) is not ok


def test_down_cat_2_structure_is_below_last_swing_high_or_sma20():
    above_both = _down_m(sma20=99.0, swings=[Swing(SWING_HIGH, 200, 99.5, 205)])
    assert any("not below the last swing high" in r for r in _down(above_both))
    assert _down(_down_m(sma20=99.0)) == []  # below the 115 swing high
    assert _down(_down_m(swings=[])) == []  # below SMA20 104


def test_down_cat_3_structure_is_below_sma20_or_sma50():
    assert _down(_down_m(sma20=99.0), category=3) == []  # still below SMA50


@pytest.mark.parametrize("category, band", [(2, (40.0, 70.0)), (3, (30.0, 75.0))])
def test_down_rsi_band_is_inclusive(category, band):
    assert _down(_down_m(rsi=band[0]), category=category) == [] and _down(_down_m(rsi=band[1]), category=category) == []
    assert _down(_down_m(rsi=band[0] - 0.01), category=category) and _down(_down_m(rsi=band[1] + 0.01), category=category)


@pytest.mark.parametrize("flags, passes", [
    ({"shortable": True, "easy_to_borrow": True}, (True, True, True)),
    ({"shortable": False, "easy_to_borrow": True}, (False, True, True)),
    ({"shortable": True, "easy_to_borrow": False}, (False, False, True)),
    ({"shortable": False, "easy_to_borrow": False}, (False, False, False)),
    ({"shortable": None, "easy_to_borrow": None}, (False, False, False)),
    (None, (False, False, False)),
])
def test_down_borrow_rule_per_category(flags, passes):
    assert tuple(borrow_ok(DOWN[c].borrow, flags) for c in (1, 2, 3)) == passes
    for c, ok in zip((2, 3), passes[1:]):
        assert any("borrow needs" in r for r in _down(flags=flags, category=c)) is not ok


def _rally(rally_at=-2, rally_volume=0.8, breakdown_volume=1.5, close_now=98.0, n=30):
    """Bars whose SMA20 is 100 throughout. The rally bar's high reaches 100.2; the as-of bar closes at close_now."""
    close = np.full(n, 99.0)
    high = close + 0.5
    volume = np.full(n, 1_000_000.0)
    high[n - 1 + rally_at] = 100.2
    volume[n - 1 + rally_at] = rally_volume * 1_000_000
    close[-1], high[-1], volume[-1] = close_now, close_now + 0.5, breakdown_volume * 1_000_000
    series = {"high": high, "low": close - 0.5, "close": close, "volume": volume, "sma20": np.full(n, 100.0)}
    return _down_m(bars=260, close=close_now, sma20=100.0, rsi=50.0, volume=volume[-1], volume_avg20=1_000_000.0,
                   swings=[], series=series)


def test_failed_rally_into_sma20_then_heavy_close_below_passes_cat_1():
    m = _rally()
    assert down.failed_rally(m, DOWN[1])["level"] == "sma20"
    assert _down(m, rs={"rs_rank": 10.0, "strength_vs_spy": -0.1}, category=1) == []


def test_failed_rally_needs_light_rally_volume_heavy_breakdown_and_a_recent_rally():
    assert down.failed_rally(_rally(rally_volume=1.0), DOWN[1]) is None  # not below the average
    assert down.failed_rally(_rally(rally_at=-5), DOWN[1]) is not None  # 5 sessions back still counts
    assert down.failed_rally(_rally(rally_at=-6), DOWN[1]) is None
    assert down.failed_rally(_rally(close_now=100.0), DOWN[1]) is None  # did not close back below
    reasons = _down(_rally(breakdown_volume=1.49), category=1)
    assert any("under 1.5x the 20-day average" in r for r in reasons)
    assert _down(_rally(breakdown_volume=1.5), category=1) == []


def test_failed_rally_into_broken_support():
    m = _rally()
    sma20 = np.full(len(m.series["close"]), 110.0)  # out of reach: only broken support can be the level
    swing = Swing(SWING_LOW, 5, 99.2, 8)  # closes at 99.0 after bar 8 broke it
    m = replace(m, swings=[swing], series={**m.series, "sma20": sma20})
    rally = down.failed_rally(m, DOWN[1])
    assert rally is not None and rally["level"] == "broken_support" and rally["level_now"] == 99.2
    unbroken = replace(m, swings=[Swing(SWING_LOW, 5, 98.5, 8)])  # no close fell below 98.5 before the rally
    assert down.failed_rally(unbroken, DOWN[1]) is None


def test_down_cat_1_needs_shortable_and_easy_to_borrow():
    assert any("borrow needs" in r for r in _down(_rally(), flags={"shortable": False, "easy_to_borrow": True},
                                                    category=1))


# --- SCANSIDE -------------------------------------------------------------------------

def _side_m(**overrides) -> Measures:
    """Measures for SCANSIDE: ADX 15, volume below its average."""
    return _m(**{"adx": 15.0, **overrides})


def _side(which, m=None, ch=None, flags=BORROW_ALL, category=1):
    default_rsi = 40.0 if which == side.LONG else 60.0
    return side.check_side(which, m or _side_m(rsi=default_rsi), ch or _channel(), flags, SIDE[category])


def test_side_reference_passes_both_sides_every_category():
    for c in (1, 2, 3):
        assert _side(side.LONG, category=c) == [] and _side(side.SHORT, category=c) == []


@pytest.mark.parametrize("category, ceiling", [(1, 20.0), (2, 25.0), (3, 30.0)])
def test_side_adx_ceiling_is_inclusive(category, ceiling):
    assert _side(side.LONG, _side_m(rsi=40.0, adx=ceiling), category=category) == []
    assert _side(side.LONG, _side_m(rsi=40.0, adx=ceiling + 0.01), category=category)


@pytest.mark.parametrize("category, band", [(1, (8.0, 22.0)), (2, (6.0, 25.0)), (3, (5.0, 30.0))])
def test_side_height_band_is_inclusive(category, band):
    for h in band:
        assert _side(side.LONG, ch=_channel(height_pct=h), category=category) == []
    for h in (band[0] - 0.01, band[1] + 0.01):
        assert _side(side.LONG, ch=_channel(height_pct=h), category=category)


def test_side_touches():
    for c in (1, 2):
        assert _side(side.LONG, ch=_channel(support_touches=1), category=c)
        assert _side(side.SHORT, ch=_channel(resistance_touches=1), category=c)
    assert _side(side.LONG, ch=_channel(support_touches=1, resistance_touches=1), category=3) == []
    assert _side(side.LONG, ch=_channel(support_touches=0), category=3)


@pytest.mark.parametrize("category, band", [(1, (0.0, 0.6)), (2, (0.0, 1.0)), (3, (-1.5, 1.5))])
def test_side_line_band_is_inclusive_on_both_sides(category, band):
    for which, key in ((side.LONG, "distance_long_atr"), (side.SHORT, "distance_short_atr")):
        for d in band:
            assert _side(which, ch=_channel(**{key: d}), category=category) == []
        for d in (band[0] - 0.01, band[1] + 0.01):
            assert _side(which, ch=_channel(**{key: d}), category=category)


@pytest.mark.parametrize("category, long_band, short_band", [
    (1, (35.0, 48.0), (52.0, 65.0)), (2, (30.0, 50.0), (50.0, 70.0)), (3, (25.0, 55.0), (45.0, 75.0)),
])
def test_side_rsi_bands_are_inclusive(category, long_band, short_band):
    for which, band in ((side.LONG, long_band), (side.SHORT, short_band)):
        for r in band:
            assert _side(which, _side_m(rsi=r), category=category) == []
        for r in (band[0] - 0.01, band[1] + 0.01):
            assert _side(which, _side_m(rsi=r), category=category)


def test_side_volume_is_below_1x_on_cat_1_below_1_2x_on_cat_2_and_free_on_cat_3():
    def m(v):
        return _side_m(rsi=40.0, volume=v)
    assert _side(side.LONG, m(999_999.0)) == [] and _side(side.LONG, m(1_000_000.0))
    assert _side(side.LONG, m(1_199_999.0), category=2) == [] and _side(side.LONG, m(1_200_000.0), category=2)
    assert _side(side.LONG, m(5_000_000.0), category=3) == []


def test_side_short_borrow_per_category():
    etb_only = {"shortable": False, "easy_to_borrow": True}
    shortable_only = {"shortable": True, "easy_to_borrow": False}
    neither = {"shortable": False, "easy_to_borrow": False}
    assert _side(side.SHORT, flags=etb_only) == [] and _side(side.SHORT, flags=etb_only, category=2) == []
    assert _side(side.SHORT, flags=shortable_only) and _side(side.SHORT, flags=shortable_only, category=2)
    assert _side(side.SHORT, flags=shortable_only, category=3) == []
    assert _side(side.SHORT, flags=neither, category=3)
    assert _side(side.LONG, flags=neither) == []  # longs need no borrow


def test_side_candidates_follow_the_financial_groups():
    inputs = _inputs(["A", "B", "C", "D"], strong={"A"}, weak={"B"}, neutral={"C"})
    assert side.candidates(inputs, SIDE[1].long_financials) == ["A"]
    assert side.candidates(inputs, SIDE[1].short_financials) == ["B"]
    assert side.candidates(inputs, SIDE[2].long_financials) == ["A", "C"]
    assert side.candidates(inputs, SIDE[2].short_financials) == ["A", "B", "C", "D"]
    assert side.candidates(inputs, SIDE[3].long_financials) == ["A", "B", "C", "D"]


# --- inputs ---------------------------------------------------------------------------

def _financials(universe_report="universe.json"):
    strong = GatePass(STRONG, [], ["A"], {}, [
        Removal("B", "diluted_eps", "diluted EPS TTM -1 is not above 0"),
        Removal("C", "operating_income", "operating income TTM -5 is not above 0"),
        Removal("D", "fundamentals_data", "no fundamentals lookup for this symbol"),
    ])
    weak = GatePass(WEAK, [], ["B"], {}, [])
    return FinancialsReport(
        as_of=AS_OF, created_at=CREATED, universe_report=universe_report, universe_created_at=CREATED.isoformat(),
        universe_survivors=4, fundamentals_dir="fx", policy={}, data_sources={}, data_freshness={},
        passes=[strong, weak], warnings=[],
    )


def test_neutral_is_a_strong_metric_failure_that_is_not_weak():
    assert neutral_names(_financials()) == {"C"}  # B is WEAK; D had no fundamentals


def test_load_inputs_reads_the_report_chain(tmp_path):
    fetched = datetime(2026, 10, 1, 22, 0, tzinfo=UTC)
    snapshot = alpaca_assets.save_snapshot(
        [{"symbol": "A", "shortable": True, "easy_to_borrow": False}, {"symbol": "B"}], tmp_path, fetched_at=fetched)
    universe = FunnelReport(
        as_of=AS_OF, created_at=CREATED, preset="DEFAULT", thresholds={}, feed="sip", assets_source=str(snapshot),
        assets_fetched_at=fetched, point_in_time=False, universe_count=4, sessions=[], spread_method="sampled",
        spread_sampling={}, stage_counts=[], survivors=["D", "C", "B", "A"], survivor_metrics={}, removed=[],
    )
    universe_path = save_universe(universe, tmp_path)
    path = save_financials(_financials(str(universe_path)), tmp_path)

    inputs = load_inputs(path)
    assert inputs.universe == ["A", "B", "C", "D"]
    assert (inputs.strong, inputs.weak, inputs.neutral) == ({"A"}, {"B"}, {"C"})
    assert inputs.borrow["A"] == {"shortable": True, "easy_to_borrow": False}
    assert inputs.borrow["B"] == {"shortable": None, "easy_to_borrow": None}
    assert inputs.sources["borrow_point_in_time"] is False
    assert any("not point in time" in w for w in inputs.warnings)


def test_relative_strength_ranks_and_compares_with_spy():
    frames = {
        "A": _frame("A", [100 + i for i in range(100)]),
        "B": _frame("B", [100.0] * 100),
        "C": _frame("C", [100 - 0.5 * i for i in range(100)]),
    }
    rs = relative_strength(frames, SPY, ["A", "B", "C", "X"])
    assert [rs[s]["rs_rank"] for s in "ABC"] == [100.0, pytest.approx(66.6667), pytest.approx(33.3333)]
    spy_return = SPY["close"].iloc[-1] / SPY["close"].iloc[-64] - 1
    assert rs["B"]["strength_vs_spy"] == pytest.approx(-spy_return, abs=1e-4)
    assert rs["X"] == {"return_63": None, "strength_vs_spy": None, "rs_rank": None}


def test_fetch_bars_drops_bars_after_as_of_and_fails_closed_without_spy():
    later = _frame("A", [1.0] * 270, end=date(2026, 10, 14))
    by_symbol, spy = fetch_bars(_inputs(["A"]), bars_fetcher=FakeBars({"A": later, BENCHMARK: SPY}))
    assert by_symbol["A"]["date"].max() <= pd.Timestamp(AS_OF) and spy is not None
    with pytest.raises(MissingBenchmark):
        fetch_bars(_inputs(["A"]), bars_fetcher=FakeBars({"A": later}))
    by_symbol, spy = fetch_bars(_inputs(["A"]), benchmark=False, bars_fetcher=FakeBars({"A": later}))
    assert spy is None and "A" in by_symbol


# --- whole runs -------------------------------------------------------------------------

def _uptrend(symbol, cat1_volume=False):
    closes = _saw(260)
    volume = np.full(260, 1_000_000.0)
    if cat1_volume:
        volume[-5:] = 600_000.0
    return _frame(symbol, closes, volume)


def test_up_run_builds_a_tickerbasket_with_every_strong_name_once(tmp_path):
    frames = {"A": _uptrend("A"), "B": _frame("B", [200 - 0.3 * i for i in range(260)]), BENCHMARK: SPY}
    fake = FakeBars(frames)
    inputs = _inputs(["A", "B", "C", "W"], strong={"A", "B", "C"})
    basket = up.run_scan(inputs, UP[2], bars_fetcher=fake, created_at=CREATED)

    assert basket.scan_name == "SCAN-UP Category 2" and basket.as_of == AS_OF
    assert basket.tickers == ["A"]
    assert sorted([*basket.tickers, *(r.ticker for r in basket.rejected)]) == basket.universe == ["A", "B", "C"]
    assert dict((r.ticker, r.reason) for r in basket.rejected)["C"] == "no bars"
    assert basket.parameters["category"] == 2 and basket.parameters["regime"] == "UP"
    assert basket.parameters["financial_gate"] == STRONG and basket.parameters["rules"]["adx_min"] == 20.0
    assert basket.parameters["rs_rank_population"] == 2 and basket.parameters["places_orders"] is False
    assert sorted(fake.requested[0]) == ["A", "B", "C", BENCHMARK, "W"]  # RS is ranked over the whole universe

    path = save_basket(basket, tmp_path)
    assert load_basket(path).to_dict() == json.loads(json.dumps(basket.to_dict()))
    assert json.loads(path.read_text())["schema_version"] == 1
    with pytest.raises(FileExistsError):
        save_basket(basket, tmp_path)


def test_up_cat_1_takes_the_same_name_once_volume_dries_up():
    frames = {"A": _uptrend("A"), "B": _uptrend("B", cat1_volume=True), BENCHMARK: SPY}
    basket = up.run_scan(_inputs(["A", "B"], strong={"A", "B"}), UP[1], bars_fetcher=FakeBars(frames),
                         created_at=CREATED)
    assert basket.tickers == ["B"]
    assert "5-session average volume" in basket.rejected[0].reason


def test_down_run_checks_borrow_on_the_snapshot():
    closes = [250 - c for c in _saw(260)]  # the uptrend mirrored: a downtrend ending in a bounce
    frames = {s: _frame(s, closes) for s in ("A", "B")} | {s: _uptrend(s) for s in ("C", "D")} | {BENCHMARK: SPY}
    # C and D are not STRONG, but they count in the RS rank: A and B tie at 37.5, under Cat 2's 40.
    inputs = _inputs(["A", "B", "C", "D"], strong={"A", "B"},
                     borrow={"A": {"shortable": True, "easy_to_borrow": True},
                             "B": {"shortable": True, "easy_to_borrow": False}})
    basket = down.run_scan(inputs, DOWN[3], bars_fetcher=FakeBars(frames), created_at=CREATED)
    assert basket.scan_name == "SCAN-DOWN Category 3"
    assert basket.tickers == ["A", "B"]  # shortable or easy_to_borrow
    basket = down.run_scan(inputs, DOWN[2], bars_fetcher=FakeBars(frames), created_at=CREATED)
    assert basket.members[0].metrics["rs_rank"] == 37.5
    assert basket.tickers == ["A"] and "borrow needs easy to borrow" in dict(
        (r.ticker, r.reason) for r in basket.rejected)["B"]
    assert basket.members[0].metrics["easy_to_borrow"] is True


def test_side_run_writes_a_long_and_a_short_basket(tmp_path):
    frames = {"L": _frame("L", _triangle(260)), "S": _frame("S", _triangle(260, end_phase=10))}
    fake = FakeBars(frames)
    inputs = _inputs(["L", "S"], borrow={"S": {"shortable": True, "easy_to_borrow": False}})
    baskets = side.run_scan(inputs, SIDE[3], bars_fetcher=fake, created_at=CREATED)

    assert BENCHMARK not in fake.requested[0]
    assert baskets["long"].scan_name == "SCAN-SIDE Long Category 3" and baskets["long"].tickers == ["L"]
    assert baskets["short"].scan_name == "SCAN-SIDE Short Category 3" and baskets["short"].tickers == ["S"]
    assert baskets["long"].parameters["pivot_confirmation"] == "not run"
    assert baskets["short"].members[0].metrics["support_touches"] >= 1
    paths = [save_basket(b, tmp_path) for b in baskets.values()]
    assert len(set(paths)) == 2


def test_side_draws_each_side_from_its_financial_groups():
    frames = {"L": _frame("L", _triangle(260)), "S": _frame("S", _triangle(260, end_phase=10))}
    inputs = _inputs(["L", "S", "N"], strong={"S"}, weak={"L"}, neutral={"N"})
    cat1 = side.run_scan(inputs, SIDE[1], bars_fetcher=FakeBars(frames), created_at=CREATED)
    assert cat1["long"].universe == ["S"] and cat1["short"].universe == ["L"]
    cat2 = side.run_scan(inputs, SIDE[2], bars_fetcher=FakeBars(frames), created_at=CREATED)
    assert cat2["long"].universe == ["N", "S"] and cat2["long"].tickers == []  # L at support is WEAK
    assert cat2["short"].universe == ["L", "N", "S"]  # Cat 2 shorts have no financial filter
    assert cat2["short"].tickers == []  # S is at resistance, but has no borrow flags


def test_category_baskets_are_named_apart_from_the_router_and_the_ui_scans():
    names = [up.scan_name(c) for c in (1, 2, 3)] + [down.scan_name(c) for c in (1, 2, 3)] + [
        side.scan_name(s, c) for s in side.SIDES for c in (1, 2, 3)]
    assert len(set(names)) == 12
    existing = ("SCAN-Upward Trend Momentum", "SCAN-Breakdown Short Candidates",
                "SCAN-LongSidewaysChannel", "SCAN-ShortSidewaysChannel")
    prefixes = ("scan-upward-trend-momentum", "scan-breakdown-short-candidates", "long_channel", "short_channel")
    for name in names:
        basket = TickerBasket(name, AS_OF, CREATED, [], {}, [], [])
        assert name not in existing and not basket_filename(basket).startswith(prefixes)


def test_the_scans_import_no_trading_client():
    import extensions.scans.category_inputs as inputs_module
    for module in (up, down, side, inputs_module, cli):
        text = open(module.__file__, encoding="utf-8").read()
        assert "TradingClient" not in text and "submit_order" not in text


# --- the runner -------------------------------------------------------------------------

def test_cli_category_defaults_to_1_and_refuses_others():
    args = cli._parser().parse_args(["--scan", "up", "--as-of", "2026-09-30"])
    assert args.category == 1
    assert cli._parser().parse_args(["--scan", "side", "--as-of", "2026-09-30", "--category", "3"]).category == 3
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["--scan", "up", "--as-of", "2026-09-30", "--category", "4"])


def test_cli_runs_the_chosen_category_and_writes_new_files(tmp_path, monkeypatch, capsys):
    report = tmp_path / "financials.json"
    report.write_text("{}")
    inputs = _inputs(["L", "S"])
    frames = {"L": _frame("L", _triangle(260)), "S": _frame("S", _triangle(260, end_phase=10))}
    seen = {}

    def fake_run(inputs_, params, cache_dir=None):
        seen["params"] = params
        return side.run_scan(inputs_, params, bars_fetcher=FakeBars(frames), created_at=CREATED)

    monkeypatch.setattr(cli, "load_inputs", lambda path, snapshot: inputs)
    monkeypatch.setattr(cli, "SCANS", {**cli.SCANS, "side": type("M", (), {"run_scan": staticmethod(fake_run)})})
    out_dir = tmp_path / "baskets"

    code = cli.main(["--scan", "side", "--financials-report", str(report), "--category", "3", "--out-dir", str(out_dir)])
    assert code == cli.EXIT_OK and seen["params"] is SIDE[3]
    out = capsys.readouterr().out
    assert "SCAN-SIDE Long Category 3 as of 2026-09-30: 1 of 2 passed" in out
    assert sorted(p.name.split("_")[0] for p in out_dir.iterdir()) == ["scan-side-long-category-3",
                                                                        "scan-side-short-category-3"]


def test_cli_without_a_financials_report_is_bad_input(tmp_path, capsys):
    code = cli.main(["--scan", "up", "--as-of", "2026-09-30", "--report-dir", str(tmp_path)])
    assert code == cli.EXIT_BAD_INPUT
    assert "run run_scanbot_financials first" in capsys.readouterr().err


def test_cli_fails_closed_without_spy(tmp_path, monkeypatch, capsys):
    report = tmp_path / "financials.json"
    report.write_text("{}")
    monkeypatch.setattr(cli, "load_inputs", lambda path, snapshot: _inputs(["A"], strong={"A"}))

    def no_spy(inputs_, params, cache_dir=None):
        return up.run_scan(inputs_, params, bars_fetcher=FakeBars({"A": _uptrend("A")}))

    monkeypatch.setattr(cli, "SCANS", {**cli.SCANS, "up": type("M", (), {"run_scan": staticmethod(no_spy)})})
    out_dir = tmp_path / "baskets"
    assert cli.main(["--scan", "up", "--financials-report", str(report), "--out-dir", str(out_dir)]) == cli.EXIT_FAILED
    assert "nothing written" in capsys.readouterr().err and not out_dir.exists()
