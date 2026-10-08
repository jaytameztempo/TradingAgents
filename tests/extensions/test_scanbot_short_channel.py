"""SCAN-ShortSidewaysChannel keeps WEAK names that are shortable and easy to borrow, with ADX <= 20,
an 8-22% channel with two touches on each side in 60 sessions, and a close within 0.6 ATR below
resistance and no more than 0.25 ATR through it. It ignores bars after as_of, counts every name once,
and never overwrites. No network.
"""

import json
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from extensions.scanbot.financials import STRONG, WEAK, FinancialsReport, GatePass
from extensions.scanbot.financials import save_report as save_financials
from extensions.scanbot.short_channel import (
    ADX_ABOVE_MAX,
    HEIGHT_TOO_LARGE,
    HEIGHT_TOO_SMALL,
    NO_BARS,
    NOT_EASY_TO_BORROW,
    NOT_SHORTABLE,
    SHORT_HISTORY,
    THROUGH_RESISTANCE,
    TOO_FAR_BELOW_RESISTANCE,
    TOO_FEW_RESISTANCE,
    TOO_FEW_SUPPORT,
    borrow_flags,
    check_borrow,
    check_channel,
    classify_short_channel,
    load_report,
    run_short_channel,
    save_report,
)
from extensions.scripts import run_scanbot_short_channel as cli

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 10, 8, 12, 5, tzinfo=UTC)
FETCHED_AFTER = datetime(2026, 10, 4, 2, 7, tzinfo=UTC)
OK = {"shortable": True, "easy_to_borrow": True}


def _triangle(n, lo=100.0, hi=112.0, half=10, end_phase=0):
    """A clean range; the last bar sits ``end_phase`` bars after a trough (``half`` puts it on a peak)."""
    step = (hi - lo) / half
    out = []
    for i in range(n):
        k = (i - (n - 1) + end_phase) % (2 * half)
        out.append(lo + step * (k if k <= half else 2 * half - k))
    return out


AT_RESISTANCE = _triangle(260, end_phase=10)
AT_SUPPORT = _triangle(260)
MID_CHANNEL = _triangle(260, end_phase=5)
NARROW = _triangle(260, hi=106.0, end_phase=10)
WIDE = _triangle(260, hi=130.0, end_phase=10)
BROKEN = AT_RESISTANCE[:-1] + [116.0]
RISING = [100 + 0.5 * i for i in range(260)]


def _frame(symbol, closes, end=AS_OF):
    closes = np.array(closes, dtype=float)
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    return pd.DataFrame({
        "symbol": symbol, "date": dates, "open": closes, "high": closes + 0.5,
        "low": closes - 0.5, "close": closes, "volume": 1_000_000,
    })


def _classify(closes, shortable=True, easy_to_borrow=True):
    return classify_short_channel(_frame("A", closes), shortable, easy_to_borrow)


def _check(**overrides):
    numbers = dict(adx=15.0, support_touches=2, resistance_touches=2, height_pct=12.0, distance_atr=-0.3)
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


def test_pivot_band_is_0_6_below_and_0_25_through():
    assert _check(distance_atr=-0.6) == [] and _check(distance_atr=0.25) == []
    assert _check(distance_atr=-0.61) == [TOO_FAR_BELOW_RESISTANCE]
    assert _check(distance_atr=0.26) == [THROUGH_RESISTANCE]


def test_both_borrow_flags_must_be_true():
    assert check_borrow(True, True)[0] == []
    assert check_borrow(False, True)[0] == [NOT_SHORTABLE]
    assert check_borrow(True, False)[0] == [NOT_EASY_TO_BORROW]
    assert check_borrow(None, None)[0] == [NOT_SHORTABLE, NOT_EASY_TO_BORROW]


def test_borrow_flags_reads_missing_as_none():
    flags = borrow_flags([{"symbol": "A", "shortable": True, "easy_to_borrow": False}, {"symbol": "B"}])
    assert flags == {"A": {"shortable": True, "easy_to_borrow": False},
                     "B": {"shortable": None, "easy_to_borrow": None}}


# --- bars --------------------------------------------------------------------

def test_range_at_resistance_passes_on_bars():
    call = _classify(AT_RESISTANCE)
    assert call.passed and call.reasons == []
    m = call.metrics
    assert m["adx_14"] <= 20 and 8 <= m["height_pct"] <= 22
    assert len(m["support_touches"]) >= 2 and len(m["resistance_touches"]) >= 2
    assert -0.6 <= m["distance_to_resistance_atr"] <= 0.25


def test_failures_on_bars_carry_the_right_reason():
    assert _classify(AT_SUPPORT).reasons == [TOO_FAR_BELOW_RESISTANCE]
    assert _classify(MID_CHANNEL).reasons == [TOO_FAR_BELOW_RESISTANCE]
    assert _classify(NARROW).reasons == [HEIGHT_TOO_SMALL]
    assert _classify(WIDE).reasons == [HEIGHT_TOO_LARGE]
    assert THROUGH_RESISTANCE in _classify(BROKEN).reasons
    trend = _classify(RISING)
    assert ADX_ABOVE_MAX in trend.reasons and TOO_FEW_SUPPORT in trend.reasons


def test_a_perfect_channel_fails_without_borrow():
    assert _classify(AT_RESISTANCE, shortable=False).reasons == [NOT_SHORTABLE]
    assert _classify(AT_RESISTANCE, easy_to_borrow=None).reasons == [NOT_EASY_TO_BORROW]


def test_short_history_and_no_bars():
    assert _classify(AT_RESISTANCE[-59:]).reasons == [SHORT_HISTORY]
    assert _classify([]).reasons == [NO_BARS]


# --- the run -----------------------------------------------------------------

def _financials(weak, strong=()):
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
    "RES": pd.concat([_frame("RES", AT_RESISTANCE), _after_as_of("RES", [80.0] * 30)], ignore_index=True),
    "HTB": _frame("HTB", AT_RESISTANCE),
    "NOSH": _frame("NOSH", AT_RESISTANCE),
    "UNK": _frame("UNK", AT_RESISTANCE),
    "SUPP": _frame("SUPP", AT_SUPPORT),
    "NARR": _frame("NARR", NARROW),
    "TRND": _frame("TRND", RISING),
    "NEW": _frame("NEW", AT_RESISTANCE[-30:]),
    "OLD": _frame("OLD", AT_RESISTANCE, end=date(2026, 9, 29)),
    "PROF": _frame("PROF", AT_RESISTANCE),
}
BORROW = {
    "RES": OK, "SUPP": OK, "NARR": OK, "TRND": OK, "NEW": OK, "OLD": OK, "GONE": OK, "PROF": OK,
    "HTB": {"shortable": True, "easy_to_borrow": False},
    "NOSH": {"shortable": False, "easy_to_borrow": True},
}
WEAK_NAMES = ("RES", "HTB", "NOSH", "UNK", "SUPP", "NARR", "TRND", "NEW", "GONE", "OLD")


def _run(weak=WEAK_NAMES, strong=("PROF",)):
    fetcher = FakeBars(FRAMES)
    report = run_short_channel(_financials(weak, strong), "financials.json", BORROW, "assets.json", FETCHED_AFTER,
                               bars_fetcher=fetcher, created_at=CREATED)
    return report, fetcher


def test_run_scans_weak_survivors_only_and_counts_every_name():
    report, fetcher = _run()
    assert fetcher.requested[0][0] == list(WEAK_NAMES)
    assert fetcher.requested[0][2] == AS_OF and fetcher.requested[0][3] == "sip"
    assert "PROF" not in report.calls  # a STRONG name never enters, even at resistance
    assert report.members == ["OLD", "RES"]
    assert report.stage_counts == {
        "weak_financials": 10, "shortable_and_easy_to_borrow": 7, "bars": 5, "adx_le_20": 4,
        "touches_2_and_2": 4, "height_8_to_22pct": 3, "at_resistance": 2,
    }
    assert report.reason_counts[NOT_SHORTABLE] == 2 and report.reason_counts[NOT_EASY_TO_BORROW] == 2
    assert report.calls["UNK"]["reasons"] == [NOT_SHORTABLE, NOT_EASY_TO_BORROW]
    assert report.basket_id == "SCAN-ShortSidewaysChannel-2026-09-30"
    assert report.mode == "SHORT_SIDE" and report.pivot_side == "SHORT" and report.financial_gate == "WEAK"


def test_bars_after_as_of_are_ignored():
    report, _ = _run()
    res = report.calls["RES"]
    assert res["passed"] and res["metrics"]["last_bar_date"] == "2026-09-30"
    assert res["metrics"]["bars"] == 260


def test_warnings_flag_borrow_snapshot_missing_names_and_stale_bars():
    report, _ = _run()
    assert any("not point in time" in w and "universe" in w for w in report.warnings)
    assert any("shortable and easy_to_borrow" in w and "not point in time" in w for w in report.warnings)
    assert any("not in the asset snapshot" in w and "UNK" in w for w in report.warnings)
    assert any("no bar on 2026-09-30: OLD" in w for w in report.warnings)


def test_report_round_trips_and_never_overwrites(tmp_path):
    report, _ = _run()
    path = save_report(report, tmp_path)
    assert path.name == "short_channel_2026-09-30_20261008T120500Z.json"
    assert load_report(path) == report
    assert json.loads(path.read_text())["policy"]["requires_shortable"] is True
    with pytest.raises(FileExistsError):
        save_report(report, tmp_path)


def test_cli_picks_the_newest_financials_report_and_writes(tmp_path, monkeypatch, capsys):
    save_financials(_financials(["RES", "SUPP"]), tmp_path)
    snapshot = tmp_path / "assets.json"
    snapshot.write_text(json.dumps({
        "snapshot_version": 1, "fetched_at": FETCHED_AFTER.isoformat(), "source": "test", "count": 2,
        "assets": [{"symbol": "RES", **OK}, {"symbol": "SUPP", **OK}],
    }))
    monkeypatch.setattr(cli, "run_short_channel",
                        lambda fin, path, borrow, src, at, cache_dir=None: run_short_channel(
                            fin, path, borrow, src, at, bars_fetcher=FakeBars(FRAMES), created_at=CREATED))
    out = tmp_path / "out"
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path), "--assets-snapshot", str(snapshot),
                     "--out-dir", str(out)]) == cli.EXIT_OK
    text = capsys.readouterr().out
    assert "SHORT_SIDE: RES" in text
    assert len(list(out.glob("short_channel_2026-09-30_*.json"))) == 1


def test_cli_without_a_financials_report_is_bad_input(tmp_path):
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path)]) == cli.EXIT_BAD_INPUT
