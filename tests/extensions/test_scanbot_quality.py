"""SCANBot quality filter checks only the 4-hour confirmed names, using the quality checks already recorded
in the pivot report: at least two of four must be true, and a missing check counts as not true. Daily and
4-hour fails are carried, never re-checked. The pivot report must be the one the 4-hour step used. No bars,
no network, and nothing is overwritten.
"""

import json
from datetime import UTC, date, datetime

import pytest

from extensions.scanbot.confirm_4h import Confirm4hReport
from extensions.scanbot.confirm_4h import save_report as save_confirm
from extensions.scanbot.pivot import (
    DOWN,
    POLICY,
    Q_CLOSE_LOCATION,
    Q_RSI,
    Q_SMA20,
    Q_VOLUME,
    UP,
    PivotReport,
)
from extensions.scanbot.pivot import save_report as save_pivot
from extensions.scanbot.quality import (
    TOO_FEW_CHECKS,
    derive_checks,
    filter_quality,
    load_report,
    run_quality,
    save_report,
)
from extensions.scripts import run_scanbot_quality as cli

AS_OF = date(2026, 9, 30)
PIVOT_CREATED = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)
CONFIRM_CREATED = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
CREATED = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def _metrics(side, *, near=True, rsi_ok=True, dry=True, off=True):
    """Metrics that re-derive to the given checks."""
    band = POLICY["rsi_bands"][side]
    return {
        "sma_20": 100.0, "sma_20_sloped_right_way": True, "distance_to_sma20_atr": 0.3 if near else 1.2,
        "atr_14": 2.0, "rsi_14": (band[0] + band[1]) / 2 if rsi_ok else band[1] + 5,
        "volume_ratio_5_20": 0.7 if dry else 1.1,
        "close_location_last_2": ([0.5, 0.5] if off else ([0.5, 0.1] if side == UP else [0.5, 0.9])),
    }


def _call(side, *, structure=True, near=True, rsi_ok=True, dry=True, off=True, quality=None):
    checks = quality or {Q_SMA20: near, Q_RSI: rsi_ok, Q_VOLUME: dry, Q_CLOSE_LOCATION: off}
    return {
        "side": side, "structure_pass": structure, "reasons": [], "detail": [],
        "quality": checks, "quality_passed": sum(1 for v in checks.values() if v),
        "metrics": _metrics(side, near=near, rsi_ok=rsi_ok, dry=dry, off=off),
    }


def _pivot(calls):
    return PivotReport(
        as_of=AS_OF, created_at=PIVOT_CREATED, trend_report="trend.json", trend_created_at=PIVOT_CREATED.isoformat(),
        assets_source="assets", assets_fetched_at=PIVOT_CREATED.isoformat(), policy=dict(POLICY), counts={},
        passed={side: sorted(s for s, c in calls[side].items() if c["structure_pass"]) for side in (UP, DOWN)},
        calls=calls,
    )


def _confirm(pivot, confirmed, fails_4h=()):
    calls = {side: {} for side in (UP, DOWN)}
    for side in (UP, DOWN):
        for s in pivot.passed[side]:
            calls[side][s] = {"side": side, "confirm_pass": s in confirmed[side], "reasons": [], "detail": [],
                              "metrics": {}}
    return Confirm4hReport(
        as_of=AS_OF, created_at=CONFIRM_CREATED, pivot_report="pivot.json",
        pivot_created_at=pivot.created_at.isoformat(), policy={}, counts={},
        confirmed={side: sorted(confirmed[side]) for side in (UP, DOWN)}, calls=calls,
        daily_fails={side: sorted(s for s, c in pivot.calls[side].items() if not c["structure_pass"])
                     for side in (UP, DOWN)},
    )


def _fixture():
    calls = {
        UP: {
            "AAA": _call(UP, rsi_ok=False, dry=False),  # 2 of 4: pass
            "BBB": _call(UP, near=False, rsi_ok=False, dry=False),  # 1 of 4: fail
            "CCC": _call(UP, near=False, rsi_ok=False, dry=False, off=False),  # 0 of 4 but fails the 4-hour check
            "DDD": _call(UP, structure=False),  # daily fail with 4 of 4 quality
        },
        DOWN: {
            "EEE": _call(DOWN),  # 4 of 4: pass
            "FFF": _call(DOWN, quality={Q_SMA20: True, Q_RSI: None, Q_VOLUME: None, Q_CLOSE_LOCATION: False}),
        },
    }
    pivot = _pivot(calls)
    return pivot, _confirm(pivot, {UP: ["AAA", "BBB"], DOWN: ["EEE", "FFF"]})


# --- the rule ----------------------------------------------------------------

def test_two_of_four_passes_and_one_fails():
    assert filter_quality(UP, _call(UP, rsi_ok=False, dry=False)).quality_pass
    one = filter_quality(UP, _call(UP, near=False, rsi_ok=False, dry=False))
    assert not one.quality_pass and one.reasons == [TOO_FEW_CHECKS] and one.checks_true == 1


def test_a_missing_check_counts_as_not_true():
    call = filter_quality(DOWN, _call(DOWN, quality={Q_SMA20: True, Q_RSI: None, Q_VOLUME: None,
                                                    Q_CLOSE_LOCATION: False}))
    assert not call.quality_pass and call.checks_true == 1
    assert any("missing" in line for line in call.detail)
    assert not filter_quality(UP, {"quality": {}, "metrics": {}}).quality_pass


def test_derive_checks_matches_the_pivot_rules():
    for side in (UP, DOWN):
        assert derive_checks(side, _metrics(side), POLICY) == dict.fromkeys(
            (Q_SMA20, Q_RSI, Q_VOLUME, Q_CLOSE_LOCATION), True)
        assert derive_checks(side, _metrics(side, near=False, rsi_ok=False, dry=False, off=False), POLICY) == \
            dict.fromkeys((Q_SMA20, Q_RSI, Q_VOLUME, Q_CLOSE_LOCATION), False)
    assert derive_checks(UP, {}, POLICY) == dict.fromkeys((Q_SMA20, Q_RSI, Q_VOLUME, Q_CLOSE_LOCATION))
    wrong_slope = {**_metrics(UP), "sma_20_sloped_right_way": False}
    assert derive_checks(UP, wrong_slope, POLICY)[Q_SMA20] is False


# --- the run -----------------------------------------------------------------

def test_only_confirmed_names_are_checked_and_fails_are_carried():
    pivot, confirm = _fixture()
    report = run_quality(confirm, "c.json", pivot, "p.json", created_at=CREATED)
    assert report.passed == {UP: ["AAA"], DOWN: ["EEE"]}
    assert set(report.calls[UP]) == {"AAA", "BBB"} and set(report.calls[DOWN]) == {"EEE", "FFF"}
    assert report.daily_fails == {UP: ["DDD"], DOWN: []}
    assert report.confirm_4h_fails == {UP: ["CCC"], DOWN: []}
    up = report.counts[UP]
    assert (up["checked_quality"], up["quality_pass"], up["quality_fail"]) == (2, 1, 1)
    assert (up["daily_fail_carried"], up["fail_4h_carried"], up["final_pass"], up["final_fail"]) == (1, 1, 1, 3)
    assert up["reason_counts"] == {TOO_FEW_CHECKS: 1}
    assert up["checks_true_histogram"] == {"0": 0, "1": 1, "2": 1, "3": 0, "4": 0}
    down = report.counts[DOWN]
    assert (down["quality_pass"], down["quality_fail"], down["final_fail"]) == (1, 1, 1)


def test_a_recorded_check_that_disagrees_with_its_metrics_is_a_warning_not_a_gate():
    pivot, confirm = _fixture()
    pivot.calls[UP]["BBB"]["quality"][Q_RSI] = True  # metrics say RSI is out of band
    report = run_quality(confirm, "c.json", pivot, "p.json", created_at=CREATED)
    assert "BBB" in report.passed[UP]
    assert any("UP BBB" in w and Q_RSI in w for w in report.warnings)


def test_no_warning_when_recorded_checks_match_metrics():
    pivot, confirm = _fixture()
    report = run_quality(confirm, "c.json", pivot, "p.json", created_at=CREATED)
    assert not any("disagrees" in w for w in report.warnings if "FFF" not in w)


def test_a_different_pivot_report_is_refused():
    pivot, confirm = _fixture()
    other = PivotReport(**{**pivot.__dict__, "created_at": datetime(2026, 10, 7, 2, 0, tzinfo=UTC)})
    with pytest.raises(ValueError, match="not the one the 4-hour report used"):
        run_quality(confirm, "c.json", other, "p.json")


def test_a_confirmed_name_without_a_daily_pass_is_refused():
    pivot, confirm = _fixture()
    bad = Confirm4hReport(**{**confirm.__dict__, "confirmed": {UP: ["AAA", "DDD"], DOWN: ["EEE"]}})
    with pytest.raises(ValueError, match="without a daily pass"):
        run_quality(bad, "c.json", pivot, "p.json")


def test_report_round_trips_and_is_never_overwritten(tmp_path):
    pivot, confirm = _fixture()
    report = run_quality(confirm, "c.json", pivot, "p.json", created_at=CREATED)
    path = save_report(report, tmp_path)
    assert path.name == "quality_2026-09-30_20261008T120000Z.json"
    assert load_report(path) == report
    with pytest.raises(FileExistsError):
        save_report(report, tmp_path)


# --- the script --------------------------------------------------------------

def test_cli_uses_the_newest_confirm_report_and_its_pivot_report(tmp_path, capsys):
    pivot, confirm = _fixture()
    pivot_path = save_pivot(pivot, tmp_path)
    confirm = Confirm4hReport(**{**confirm.__dict__, "pivot_report": str(pivot_path)})
    save_confirm(confirm, tmp_path)
    out_dir = tmp_path / "out"
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path), "--out-dir", str(out_dir)]) == 0
    printed = capsys.readouterr().out
    assert "quality pass 1, fail 1" in printed and "PASSED: AAA" in printed
    written = json.loads(next(out_dir.glob("quality_2026-09-30_*.json")).read_text(encoding="utf-8"))
    assert written["passed"] == {UP: ["AAA"], DOWN: ["EEE"]}


def test_cli_bad_input(tmp_path, capsys):
    assert cli.main(["--as-of", "2026-13-01"]) == cli.EXIT_BAD_INPUT
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path)]) == cli.EXIT_BAD_INPUT
    assert "no 4-hour report" in capsys.readouterr().err
