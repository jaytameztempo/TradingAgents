"""SCAN-LongSidewaysChannel keeps STRONG names with ADX <= 20, an 8-22% channel with two touches
on each side in 60 sessions, and a close within 0.6 ATR above support and no more than 0.25 ATR
through it. It ignores bars after as_of, counts every name once, and never overwrites. No network.
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
)
from extensions.scripts import run_scanbot_long_channel as cli

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
}


def _run(strong=("SUPP", "MID", "NARR", "TRND", "NEW", "GONE", "OLD"), weak=("LOSS",)):
    fetcher = FakeBars(FRAMES)
    return run_long_channel(_financials(strong, weak), "financials.json", bars_fetcher=fetcher,
                            created_at=CREATED), fetcher


def test_run_scans_strong_survivors_only_and_counts_every_name():
    report, fetcher = _run()
    assert fetcher.requested[0][0] == ["SUPP", "MID", "NARR", "TRND", "NEW", "GONE", "OLD"]
    assert fetcher.requested[0][2] == AS_OF and fetcher.requested[0][3] == "sip"
    assert "LOSS" not in report.calls  # a WEAK name never enters, even at support
    assert report.members == ["OLD", "SUPP"]
    assert report.stage_counts == {
        "strong_financials": 7, "bars": 5, "adx_le_20": 4, "touches_2_and_2": 4,
        "height_8_to_22pct": 3, "at_support": 2,
    }
    assert report.reason_counts[NO_BARS] == 1 and report.reason_counts[SHORT_HISTORY] == 1
    assert report.basket_id == "SCAN-LongSidewaysChannel-2026-09-30"
    assert report.mode == "LONG_SIDE" and report.pivot_side == "LONG"


def test_bars_after_as_of_are_ignored():
    report, _ = _run()
    supp = report.calls["SUPP"]
    assert supp["passed"] and supp["metrics"]["last_bar_date"] == "2026-09-30"
    assert supp["metrics"]["bars"] == 260


def test_warnings_carry_point_in_time_and_flag_a_missing_last_bar():
    report, _ = _run()
    assert any("not point in time" in w for w in report.warnings)
    assert any("no bar on 2026-09-30: OLD" in w for w in report.warnings)


def test_report_round_trips_and_never_overwrites(tmp_path):
    report, _ = _run()
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
                                                                           created_at=CREATED))
    out = tmp_path / "out"
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path), "--out-dir", str(out)]) == cli.EXIT_OK
    text = capsys.readouterr().out
    assert "LONG_SIDE: SUPP" in text
    assert len(list(out.glob("long_channel_2026-09-30_*.json"))) == 1


def test_cli_without_a_financials_report_is_bad_input(tmp_path):
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path)]) == cli.EXIT_BAD_INPUT
