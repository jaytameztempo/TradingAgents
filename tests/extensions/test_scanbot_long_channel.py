"""SCAN-LongSidewaysChannel keeps STRONG names with ADX <= 20, an 8-22% channel with two touches
on each side in 60 sessions, a close within 0.6 ATR above support and no more than 0.25 ATR
through it, and a pivot confirmation (a daily close reclaim or a 4-hour higher low). It does not
run the TTM Squeeze gate. It ignores bars after as_of, counts every name once, and never
overwrites. No network.
"""

import json
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from extensions.scanbot.financials import STRONG, WEAK, FinancialsReport, GatePass
from extensions.scanbot.financials import save_report as save_financials
from extensions.scanbot.long_channel import (
    ADX_ABOVE_MAX,
    HEIGHT_TOO_LARGE,
    HEIGHT_TOO_SMALL,
    NO_BARS,
    NO_PIVOT_CONFIRMATION,
    SHORT_HISTORY,
    THROUGH_SUPPORT,
    TOO_FAR_ABOVE_SUPPORT,
    TOO_FEW_RESISTANCE,
    TOO_FEW_SUPPORT,
    check_channel,
    classify_long_channel,
    load_report,
    run_long_channel,
    save_report,
    stop_swing,
)
from extensions.scanbot.pivot import SWING_LOW, Swing
from extensions.scripts import run_scanbot_long_channel as cli
from tests.extensions.test_pivot_confirm import HIGHER_LOWS, four_hour_frame

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 10, 7, 12, 5, tzinfo=UTC)


def _triangle(n, lo=100.0, hi=112.0, half=10, end_phase=0):
    """A clean range; the last bar sits ``end_phase`` bars after a trough."""
    step = (hi - lo) / half
    out = []
    for i in range(n):
        k = (i - (n - 1) + end_phase) % (2 * half)
        out.append(lo + step * (k if k <= half else 2 * half - k))
    return out


AT_SUPPORT = _triangle(260)  # 13% channel, close 0.29 ATR above support, ADX about 19.5
MID_CHANNEL = _triangle(260, end_phase=5)
NARROW = _triangle(260, hi=106.0)
WIDE = _triangle(260, hi=130.0)
BROKEN = _triangle(259) + [97.0]
RISING = [100 + 0.5 * i for i in range(260)]
# At support, but the last daily low at support (2026-09-22) is 6 sessions back: no close reclaim.
NO_RECLAIM = _triangle(254, half=6) + [101.0, 100.4, 100.9, 100.3, 100.8, 100.3]


def _frame(symbol, closes, end=AS_OF):
    closes = np.array(closes, dtype=float)
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    return pd.DataFrame({
        "symbol": symbol, "date": dates, "open": closes, "high": closes + 0.5,
        "low": closes - 0.5, "close": closes, "volume": 1_000_000,
    })


def _check(**overrides):
    numbers = dict(adx=15.0, support_touches=2, resistance_touches=2, height_pct=12.0, distance_atr=0.3)
    numbers.update(overrides)
    return check_channel(**numbers)[0]


# --- the rules ---------------------------------------------------------------

def test_all_rules_met_is_a_pass():
    assert _check() == []


def test_adx_20_passes_and_just_over_fails():
    assert _check(adx=20.0) == []
    assert _check(adx=20.01) == [ADX_ABOVE_MAX]


def test_height_band_is_inclusive():
    assert _check(height_pct=8.0) == [] and _check(height_pct=22.0) == []
    assert _check(height_pct=7.99) == [HEIGHT_TOO_SMALL]
    assert _check(height_pct=22.01) == [HEIGHT_TOO_LARGE]


def test_two_touches_each_side_are_required():
    assert _check(support_touches=1) == [TOO_FEW_SUPPORT]
    assert _check(resistance_touches=1) == [TOO_FEW_RESISTANCE]
    assert _check(support_touches=1, resistance_touches=0) == [TOO_FEW_SUPPORT, TOO_FEW_RESISTANCE]


def test_pivot_band_is_0_6_above_and_0_25_through():
    assert _check(distance_atr=0.6) == [] and _check(distance_atr=-0.25) == []
    assert _check(distance_atr=0.61) == [TOO_FAR_ABOVE_SUPPORT]
    assert _check(distance_atr=-0.26) == [THROUGH_SUPPORT]


# --- bars --------------------------------------------------------------------

def test_range_at_support_passes_on_bars():
    call = classify_long_channel(_frame("A", AT_SUPPORT))
    assert call.passed and call.reasons == []
    m = call.metrics
    assert m["adx_14"] <= 20 and 8 <= m["height_pct"] <= 22
    assert len(m["support_touches"]) >= 2 and len(m["resistance_touches"]) >= 2
    assert -0.25 <= m["distance_to_support_atr"] <= 0.6


def test_failures_on_bars_carry_the_right_reason():
    assert classify_long_channel(_frame("A", MID_CHANNEL)).reasons == [TOO_FAR_ABOVE_SUPPORT]
    assert classify_long_channel(_frame("A", NARROW)).reasons == [HEIGHT_TOO_SMALL]
    assert classify_long_channel(_frame("A", WIDE)).reasons == [HEIGHT_TOO_LARGE]
    assert THROUGH_SUPPORT in classify_long_channel(_frame("A", BROKEN)).reasons
    trend = classify_long_channel(_frame("A", RISING))
    assert ADX_ABOVE_MAX in trend.reasons and TOO_FEW_SUPPORT in trend.reasons


def test_touches_come_only_from_the_last_60_sessions():
    # A wide range long ago, then a flat line: no swings inside the window.
    call = classify_long_channel(_frame("A", _triangle(150, hi=112.0) + [106.0] * 70))
    assert TOO_FEW_SUPPORT in call.reasons and TOO_FEW_RESISTANCE in call.reasons


def test_short_history_and_no_bars():
    assert classify_long_channel(_frame("A", AT_SUPPORT[-59:])).reasons == [SHORT_HISTORY]
    assert classify_long_channel(_frame("A", [])).reasons == [NO_BARS]


def _lows(*pairs):
    """Confirmed swing lows from (bar index, price), each confirmed three bars later."""
    return [Swing(SWING_LOW, i, p, i + 3) for i, p in pairs]


def _stop_low(swings, support=100.0, atr=2.0, bars=130):
    return stop_swing(_frame("A", [100.0] * bars), swings, SWING_LOW, support, atr)


def test_the_stop_swing_low_is_the_highest_clustered_swing_low_below_support():
    """98 is below support but alone (3 from 95, the nearest); 95 and 94 are within 1 ATR of each other, and 95 is
    the higher. 101 is above support and is never the pick."""
    frame = _frame("A", [100.0] * 130)
    swing = _stop_low(_lows((50, 95.0), (70, 94.0), (100, 98.0), (110, 101.0)))
    assert (swing["price"], swing["window_sessions"], swing["rule"]) == (95.0, 120, "clustered_within_3atr_beyond_line")
    assert (swing["date"], swing["confirmed_on"]) == (frame["date"].iloc[50].date().isoformat(),
                                                      frame["date"].iloc[53].date().isoformat())
    assert swing["partner"]["price"] == 94.0


def test_the_partner_low_need_not_be_below_support_and_1_atr_is_inclusive():
    """98's only neighbour is 100, at support (not below it) and exactly 1 ATR away: it counts. At 1.99 ATR it does
    not, and 98 alone is no stop swing."""
    swings = _lows((100, 98.0), (110, 100.0))
    assert _stop_low(swings)["price"] == 98.0 and _stop_low(swings)["partner"]["price"] == 100.0
    assert _stop_low(swings, atr=1.99)["price"] is None


def test_swing_lows_outside_the_120_sessions_are_neither_picked_nor_partners():
    """Of 130 bars the window starts at bar 10: the 95.5 at bar 5 does not partner the 95 at bar 50."""
    assert _stop_low(_lows((5, 95.5), (50, 95.0)))["price"] is None
    assert _stop_low(_lows((10, 95.5), (50, 95.0)))["price"] == 95.5


def test_on_a_tie_the_most_recent_swing_low_is_picked():
    frame = _frame("A", [100.0] * 130)
    swing = _stop_low(_lows((40, 95.0), (60, 95.0)))
    assert swing["date"] == frame["date"].iloc[60].date().isoformat()


def test_no_stop_swing_low_without_support_or_atr():
    swings = _lows((50, 95.0), (70, 94.0))
    empty = {"price": None, "date": None, "confirmed_on": None, "window_sessions": 120,
             "rule": "clustered_within_3atr_beyond_line", "partner": None}
    assert _stop_low(swings, support=None) == empty and _stop_low(swings, atr=float("nan")) == empty


def test_a_clustered_swing_low_more_than_3_atr_below_support_is_discarded():
    """95 and 94 cluster, but with ATR 1.5 the 95 is 3.33 ATR below support: no stop swing, no fallback."""
    assert _stop_low(_lows((50, 95.0), (70, 94.0)), atr=1.5)["price"] is None


def test_a_swing_low_exactly_3_atr_below_support_is_kept():
    """With ATR 2, 94 is exactly 3 ATR below support: it counts. Its partner 92.5 (0.75 ATR away) is 3.75 ATR
    below support; the limit is on the pick, not the partner."""
    assert _stop_low(_lows((50, 94.0), (70, 92.5)))["price"] == 94.0


def test_the_3_atr_limit_keeps_the_highest_clustered_low_when_a_deeper_one_is_discarded():
    """97 and 96 cluster within 3 ATR; 90 and 89 cluster too but are 5 ATR down. The pick stays 97."""
    swing = _stop_low(_lows((30, 90.0), (40, 89.0), (50, 97.0), (70, 96.0)))
    assert swing["price"] == 97.0 and swing["partner"]["price"] == 96.0


def test_classify_records_the_clustered_stop_swing_low_from_the_bars():
    """Two dips 100 and 80 sessions back, 1 apart, are below support (99.5, ATR 1.7) within 3 ATR and cluster: the
    higher is the stop swing. The same dips more than 3 ATR down, one dip alone, or a clean range whose lows all sit at
    support, have none. Recorded only: the call does not change."""
    closes = list(AT_SUPPORT)
    closes[-100], closes[-80] = 96.0, 97.0
    frame = _frame("A", closes)
    call = classify_long_channel(frame)
    swing = call.metrics["stop_swing_low"]
    assert (swing["price"], swing["window_sessions"]) == (96.5, 120)
    assert swing["date"] == frame["date"].iloc[-80].date().isoformat()
    assert swing["partner"]["price"] == 95.5 and swing["confirmed_on"] > swing["date"]
    deep = list(AT_SUPPORT)
    deep[-100], deep[-80] = 90.0, 91.0
    assert classify_long_channel(_frame("A", deep)).metrics["stop_swing_low"]["price"] is None
    lone = list(AT_SUPPORT)
    lone[-100] = 96.0
    assert classify_long_channel(_frame("A", lone)).metrics["stop_swing_low"]["price"] is None
    clean = classify_long_channel(_frame("A", AT_SUPPORT))
    assert clean.metrics["stop_swing_low"]["price"] is None
    assert call.passed == clean.passed
    flat = classify_long_channel(_frame("A", [106.0] * 100)).metrics["stop_swing_low"]
    assert (flat["price"], flat["window_sessions"]) == (None, 100)


def test_classify_records_the_daily_pivot_and_the_close_reclaim():
    at_support = classify_long_channel(_frame("A", AT_SUPPORT)).metrics["pivot_confirmation"]
    assert at_support["close_reclaim"]["passed"] and at_support["pivot"]["date"] == "2026-09-30"
    late = classify_long_channel(_frame("A", NO_RECLAIM))
    assert late.passed  # the channel rules; the confirmation is the run's step
    assert not late.metrics["pivot_confirmation"]["close_reclaim"]["passed"]
    assert late.metrics["pivot_confirmation"]["pivot"] == {"date": "2026-09-22", "price": 99.5,
                                                           "after_swing": "2026-09-14"}


# --- the run -----------------------------------------------------------------

def _financials(strong, weak=()):
    def gate(name, survivors):
        return GatePass(name, [], list(survivors), {}, [])
    return FinancialsReport(
        as_of=AS_OF, created_at=CREATED, universe_report="universe.json", universe_created_at=CREATED.isoformat(),
        universe_survivors=len(strong) + len(weak), fundamentals_dir="fx", policy={}, data_sources={},
        data_freshness={}, passes=[gate(STRONG, strong), gate(WEAK, weak)],
        warnings=["the universe report is not point in time (its asset list was fetched after as_of)"],
    )


class FakeBars:
    def __init__(self, frames):
        self.frames = frames
        self.requested = []

    def __call__(self, symbols, start, end, *, feed, cache_dir=None):
        self.requested.append((list(symbols), start, end, feed))
        frames = [self.frames[s] for s in symbols if s in self.frames]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            {"symbol": [], "date": pd.to_datetime([]), "high": [], "low": [], "close": []})


def _after_as_of(symbol, closes):
    """A frame that runs past as_of: these later bars must be ignored."""
    later = pd.bdate_range(start=pd.Timestamp(AS_OF) + pd.Timedelta(days=1), periods=len(closes))
    frame = _frame(symbol, closes, end=later[-1])
    frame["date"] = later
    return frame


FRAMES = {
    "SUPP": pd.concat([_frame("SUPP", AT_SUPPORT), _after_as_of("SUPP", [150.0] * 30)], ignore_index=True),
    "MID": _frame("MID", MID_CHANNEL),
    "NARR": _frame("NARR", NARROW),
    "TRND": _frame("TRND", RISING),
    "NEW": _frame("NEW", AT_SUPPORT[-30:]),
    "OLD": _frame("OLD", AT_SUPPORT, end=date(2026, 9, 29)),
    "LOSS": _frame("LOSS", AT_SUPPORT),
    "HL": _frame("HL", NO_RECLAIM),
    "NOHL": _frame("NOHL", NO_RECLAIM),
}
FOUR_HOUR = {
    # A higher low after the 2026-09-22 pivot; the bars after as_of would break it and must be ignored.
    "HL": pd.concat([four_hour_frame("HL", HIGHER_LOWS), four_hour_frame("HL", [90.0, 91.0], start="2026-10-01")],
                    ignore_index=True),
    "SUPP": four_hour_frame("SUPP", [101.0, 100.0, 99.5]),  # no higher low: SUPP passes on the reclaim only
}
STRONG_NAMES = ("SUPP", "MID", "NARR", "TRND", "NEW", "GONE", "OLD", "HL", "NOHL")


def _run(strong=STRONG_NAMES, weak=("LOSS",)):
    fetcher, four_hour = FakeBars(FRAMES), FakeBars(FOUR_HOUR)
    report = run_long_channel(_financials(strong, weak), "financials.json", bars_fetcher=fetcher,
                              four_hour_fetcher=four_hour, created_at=CREATED)
    return report, fetcher, four_hour


def test_run_scans_strong_survivors_only_and_counts_every_name():
    report, fetcher, _ = _run()
    assert fetcher.requested[0][0] == list(STRONG_NAMES)
    assert fetcher.requested[0][2] == AS_OF and fetcher.requested[0][3] == "sip"
    assert "LOSS" not in report.calls  # a WEAK name never enters, even at support
    assert report.members == ["HL", "OLD", "SUPP"]
    assert report.stage_counts == {
        "strong_financials": 9, "bars": 7, "adx_le_20": 6, "touches_2_and_2": 6,
        "height_8_to_22pct": 5, "at_support": 4, "pivot_confirmed": 3,
    }
    assert report.reason_counts[NO_BARS] == 1 and report.reason_counts[SHORT_HISTORY] == 1
    assert report.reason_counts[NO_PIVOT_CONFIRMATION] == 1
    assert report.basket_id == "SCAN-LongSidewaysChannel-2026-09-30"
    assert report.mode == "LONG_SIDE" and report.pivot_side == "LONG"


def test_4hour_bars_are_read_for_channel_passes_only():
    _, _, four_hour = _run()
    assert four_hour.requested == [(["HL", "NOHL", "OLD", "SUPP"], date(2026, 6, 2), AS_OF, "sip")]


def test_a_channel_pass_needs_the_reclaim_or_the_4hour_higher_low():
    report, _, _ = _run()
    by = {s: report.calls[s]["metrics"]["pivot_confirmation"]["passed_by"] for s in ("SUPP", "HL", "NOHL")}
    assert by == {"SUPP": ["close_reclaim"], "HL": ["four_hour_turn"], "NOHL": []}
    hl = report.calls["HL"]["metrics"]["pivot_confirmation"]["four_hour_turn"]
    assert hl["anchor_price"] == 99.5 and hl["turn_price"] == 100.4
    nohl = report.calls["NOHL"]
    assert not nohl["passed"] and nohl["reasons"] == [NO_PIVOT_CONFIRMATION]
    assert nohl["detail"][0].startswith("no close reclaim") and nohl["detail"][1].startswith("no 4-hour higher low")
    assert report.calls["MID"]["metrics"]["pivot_confirmation"]["four_hour_turn"] is None  # never read


def test_the_scan_runs_no_squeeze_gate():
    report, _, _ = _run()
    assert not any("squeeze" in stage for stage in report.stage_counts)
    assert not any("squeeze" in code for code in report.reason_counts)
    assert not any("squeeze" in key for key in report.policy)
    assert all("squeeze" not in call["metrics"] for call in report.calls.values())


def test_bars_after_as_of_are_ignored():
    report, _, _ = _run()
    supp = report.calls["SUPP"]
    assert supp["passed"] and supp["metrics"]["last_bar_date"] == "2026-09-30"
    assert supp["metrics"]["bars"] == 260


def test_warnings_carry_point_in_time_and_flag_a_missing_last_bar():
    report, _, _ = _run()
    assert any("not point in time" in w for w in report.warnings)
    assert any("no bar on 2026-09-30: OLD" in w for w in report.warnings)


def test_report_round_trips_and_never_overwrites(tmp_path):
    report, _, _ = _run()
    path = save_report(report, tmp_path)
    assert path.name == "long_channel_2026-09-30_20261007T120500Z.json"
    assert load_report(path) == report
    assert json.loads(path.read_text())["policy"]["adx_max"] == 20.0
    with pytest.raises(FileExistsError):
        save_report(report, tmp_path)


def test_cli_picks_the_newest_financials_report_and_writes(tmp_path, monkeypatch, capsys):
    save_financials(_financials(["SUPP", "MID"]), tmp_path)
    monkeypatch.setattr(cli, "run_long_channel",
                        lambda fin, path, cache_dir=None: run_long_channel(fin, path, bars_fetcher=FakeBars(FRAMES),
                                                                           four_hour_fetcher=FakeBars(FOUR_HOUR),
                                                                           created_at=CREATED))
    out = tmp_path / "out"
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path), "--out-dir", str(out)]) == cli.EXIT_OK
    text = capsys.readouterr().out
    assert "LONG_SIDE: SUPP" in text
    assert len(list(out.glob("long_channel_2026-09-30_*.json"))) == 1


def test_cli_without_a_financials_report_is_bad_input(tmp_path):
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path)]) == cli.EXIT_BAD_INPUT
