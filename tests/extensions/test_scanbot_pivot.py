"""SCANBot step 4 checks UP names for a bottom pivot and DOWN names for a top pivot: ZigZag 5% swings
confirmed on close, a 6-35% impulse, a 35-50% give-back, the right side of SMA50, a 3-15 session leg,
and easy_to_borrow on DOWN. Missing structure fails. Quality is recorded, never required. Bars after
as_of are ignored, every name is counted once, and nothing is overwritten. No network.
"""

import json
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from extensions.market_data.alpaca_assets import save_snapshot
from extensions.scanbot.pivot import (
    DOWN,
    IMPULSE_TOO_LARGE,
    IMPULSE_TOO_SMALL,
    LEG_TOO_LONG,
    LEG_TOO_SHORT,
    NO_BARS,
    NO_IMPULSE,
    NOT_EASY_TO_BORROW,
    Q_VOLUME,
    QUALITY_CHECKS,
    SHORT_HISTORY,
    SWING_HIGH,
    SWING_LOW,
    UP,
    WRONG_SIDE_SMA50,
    ZONE_TOO_DEEP,
    ZONE_TOO_SHALLOW,
    borrow_flags,
    check_structure,
    classify_pivot,
    load_report,
    run_pivot,
    save_report,
    wilder_atr,
    wilder_rsi,
    zigzag_swings,
)
from extensions.scanbot.trend import TrendReport
from extensions.scanbot.trend import save_report as save_trend
from extensions.scripts import run_scanbot_pivot as cli

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 10, 6, 12, 5, tzinfo=UTC)
FETCHED_AFTER = datetime(2026, 10, 4, 2, 7, tzinfo=UTC)


def _path(start, *legs):
    """Closes from ``start`` through each (sessions, step-per-session) leg."""
    closes = [float(start)]
    for sessions, step in legs:
        closes += [closes[-1] + step * (k + 1) for k in range(sessions)]
    return closes


# Rise, a 6-point dip that confirms a swing high, a 98 -> 118 impulse, then a pullback.
BASE = ((60, 0.4), (6, -1.0), (10, 2.0))
UP_PASS = _path(80, *BASE, (5, -1.68))  # back to 109.6: about 42% of the impulse, 5 sessions
UP_SHALLOW = _path(80, *BASE, (5, -0.8))  # 114: under 5% off the high, so the high is not confirmed
UP_DEEP = _path(80, *BASE, (5, -2.4))  # 106: about 60%
UP_LONG = _path(80, *BASE, (20, -0.42))  # 109.6 over 20 sessions
UP_SHORT = _path(80, *BASE, (2, -4.2))  # 109.6 over 2 sessions
UP_HUGE = _path(80, *BASE[:2], (10, 5.0), (5, -4.0))  # 98 -> 148 is a 50% impulse


def _mirror(closes):
    return [200 - c for c in closes]


DOWN_PASS = _mirror(UP_PASS)
DOWN_SHALLOW = _mirror(_path(80, *BASE, (5, -0.7)))  # 85.5: under 5% off the 81.7 low, so not confirmed


def _frame(symbol, closes, end=AS_OF, volume=None):
    closes = np.array(closes, dtype=float)
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    volume = np.full(len(closes), 1_000_000.0) if volume is None else np.asarray(volume, dtype=float)
    return pd.DataFrame({
        "symbol": symbol, "date": dates, "open": closes, "high": closes + 0.3,
        "low": closes - 0.3, "close": closes, "volume": volume,
    })


# --- indicators and swings ---------------------------------------------------

def test_atr_and_rsi():
    flat = pd.Series([10.0] * 30)
    atr = wilder_atr(flat + 1, flat - 1, flat)
    assert atr.iloc[:14].isna().all() and atr.iloc[14] == pytest.approx(2.0) and atr.iloc[-1] == pytest.approx(2.0)
    assert wilder_rsi(pd.Series(np.arange(30, dtype=float))).iloc[-1] == 100.0
    zigzag = pd.Series([10.0 + (1 if i % 2 else 0) for i in range(60)])
    assert wilder_rsi(zigzag).iloc[-1] == pytest.approx(50.0, abs=2)


def test_zigzag_alternates_and_ignores_the_pending_extreme():
    frame = _frame("A", UP_PASS)
    swings = zigzag_swings(frame["high"], frame["low"], frame["close"])
    assert [s.kind for s in swings][-3:] == [SWING_HIGH, SWING_LOW, SWING_HIGH]
    assert all(a.kind != b.kind for a, b in zip(swings, swings[1:]))
    assert swings[-1].price == pytest.approx(118.3) and swings[-2].price == pytest.approx(97.7)
    assert all(s.confirmed_index > s.index for s in swings)
    shallow = _frame("A", UP_SHALLOW)
    assert zigzag_swings(shallow["high"], shallow["low"], shallow["close"])[-1].kind == SWING_LOW


def test_a_swing_is_confirmed_by_the_close_not_the_intraday_low():
    high = [100, 110, 109, 109]
    low = [99, 109, 103, 103]  # 6% under the high intraday
    close = [100, 110, 106, 106]  # but closes only 3.6% under it
    assert [s.kind for s in zigzag_swings(high, low, close)] == [SWING_LOW]
    close[-1] = 104.5  # 5% under 110 is 104.5
    swings = zigzag_swings(high, low, close)
    assert [s.kind for s in swings] == [SWING_LOW, SWING_HIGH] and swings[-1].confirmed_index == 3


# --- the rules ---------------------------------------------------------------

def _check(side=UP, **overrides):
    numbers = dict(impulse_pct=20.0, retrace_pct=42.0, close=110.0, sma50=105.0, leg_sessions=5, easy_to_borrow=True)
    if side == DOWN:
        numbers.update(close=95.0)
    numbers.update(overrides)
    return check_structure(side, **numbers)[0]


def test_boundaries_are_inclusive():
    for side in (UP, DOWN):
        assert _check(side) == []
        assert _check(side, impulse_pct=6.0) == [] and _check(side, impulse_pct=35.0) == []
        assert _check(side, retrace_pct=35.0) == [] and _check(side, retrace_pct=50.0) == []
        assert _check(side, leg_sessions=3) == [] and _check(side, leg_sessions=15) == []


def test_just_outside_each_boundary_fails():
    assert _check(impulse_pct=5.99) == [IMPULSE_TOO_SMALL]
    assert _check(impulse_pct=35.01) == [IMPULSE_TOO_LARGE]
    assert _check(retrace_pct=34.99) == [ZONE_TOO_SHALLOW]
    assert _check(retrace_pct=50.01) == [ZONE_TOO_DEEP]
    assert _check(leg_sessions=2) == [LEG_TOO_SHORT]
    assert _check(leg_sessions=16) == [LEG_TOO_LONG]
    assert _check(impulse_pct=40.0, retrace_pct=60.0, leg_sessions=1) == [
        IMPULSE_TOO_LARGE, ZONE_TOO_DEEP, LEG_TOO_SHORT]


def test_close_must_be_strictly_on_the_trend_side_of_sma50():
    assert _check(close=105.0) == [WRONG_SIDE_SMA50]
    assert _check(DOWN, close=105.0) == [WRONG_SIDE_SMA50]
    assert _check(DOWN, close=104.99) == []


def test_down_needs_easy_to_borrow_and_up_does_not():
    assert _check(DOWN, easy_to_borrow=False) == [NOT_EASY_TO_BORROW]
    assert _check(DOWN, easy_to_borrow=None) == [NOT_EASY_TO_BORROW]
    assert _check(UP, easy_to_borrow=None) == []


# --- bars --------------------------------------------------------------------

def test_up_and_down_pass_on_bars():
    up = classify_pivot(UP, _frame("A", UP_PASS))
    assert up.structure_pass and up.reasons == []
    assert up.metrics["impulse_pct"] == pytest.approx(21.08, abs=0.01)
    assert 35 <= up.metrics["retrace_pct"] <= 50 and up.metrics["leg_sessions"] == 5
    down = classify_pivot(DOWN, _frame("A", DOWN_PASS), easy_to_borrow=True)
    assert down.structure_pass and 35 <= down.metrics["retrace_pct"] <= 50 and down.metrics["leg_sessions"] == 5
    assert classify_pivot(DOWN, _frame("A", DOWN_PASS), easy_to_borrow=False).reasons == [NOT_EASY_TO_BORROW]


def test_each_broken_structure_fails_with_its_reason():
    assert classify_pivot(UP, _frame("A", UP_SHALLOW)).reasons == [NO_IMPULSE]
    assert classify_pivot(UP, _frame("A", UP_DEEP)).reasons == [ZONE_TOO_DEEP]
    assert classify_pivot(UP, _frame("A", UP_LONG)).reasons == [LEG_TOO_LONG]
    assert classify_pivot(UP, _frame("A", UP_SHORT)).reasons == [LEG_TOO_SHORT]
    assert IMPULSE_TOO_LARGE in classify_pivot(UP, _frame("A", UP_HUGE)).reasons
    # An UP name on a DOWN shape has no low-to-high impulse as its last swing.
    assert classify_pivot(UP, _frame("A", DOWN_PASS)).reasons == [NO_IMPULSE]
    assert classify_pivot(DOWN, _frame("A", DOWN_SHALLOW), easy_to_borrow=True).reasons == [NO_IMPULSE]
    assert classify_pivot(DOWN, _frame("A", DOWN_SHALLOW), easy_to_borrow=None).reasons == [
        NO_IMPULSE, NOT_EASY_TO_BORROW]


def test_short_history_and_no_bars():
    assert classify_pivot(UP, _frame("A", UP_PASS[-49:])).reasons == [SHORT_HISTORY]
    assert classify_pivot(UP, _frame("A", [])).reasons == [NO_BARS]
    assert classify_pivot(DOWN, _frame("A", []), easy_to_borrow=False).reasons == [NO_BARS, NOT_EASY_TO_BORROW]


def test_quality_is_recorded_but_not_required():
    quiet = [1_000_000.0] * (len(UP_PASS) - 5) + [500_000.0] * 5
    call = classify_pivot(UP, _frame("A", UP_PASS, volume=quiet))
    assert set(call.quality) == set(QUALITY_CHECKS) and call.quality[Q_VOLUME] is True
    assert call.metrics["volume_ratio_5_20"] == pytest.approx(500_000 / 875_000, abs=1e-4)
    loud = classify_pivot(UP, _frame("A", UP_PASS))
    assert loud.quality[Q_VOLUME] is False
    assert loud.structure_pass and call.structure_pass
    assert call.quality_passed == sum(bool(v) for v in call.quality.values())


# --- the run -----------------------------------------------------------------

def _trend(up, down):
    return TrendReport(
        as_of=AS_OF, created_at=CREATED, financials_report="financials.json",
        financials_created_at=CREATED.isoformat(), policy={}, entered=len(up) + len(down),
        label_counts={"UP": len(up), "DOWN": len(down), "UNCLASSIFIED": 0}, reason_counts={}, tangled_only=0,
        up=sorted(up), down=sorted(down), classifications={},
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
            {"symbol": [], "date": pd.to_datetime([]), "high": [], "low": [], "close": [], "volume": []})


def _after_as_of(symbol, closes):
    later = pd.bdate_range(start=pd.Timestamp(AS_OF) + pd.Timedelta(days=1), periods=len(closes))
    frame = _frame(symbol, closes, end=later[-1])
    frame["date"] = later
    return frame


FRAMES = {
    # Later bars would turn the pullback into a breakdown; they must be ignored.
    "UPP": pd.concat([_frame("UPP", UP_PASS), _after_as_of("UPP", [60.0] * 10)], ignore_index=True),
    "DEEP": _frame("DEEP", UP_DEEP),
    "DWN": _frame("DWN", DOWN_PASS),
    "HTB": _frame("HTB", DOWN_PASS),
    "OLD": _frame("OLD", UP_PASS, end=date(2026, 9, 29)),
}
BORROW = {"DWN": True, "HTB": False}


def _run(up=("UPP", "DEEP", "GONE", "OLD"), down=("DWN", "HTB", "NOSNAP")):
    fetcher = FakeBars(FRAMES)
    report = run_pivot(_trend(up, down), "trend.json", BORROW, "assets.json", FETCHED_AFTER,
                       bars_fetcher=fetcher, created_at=CREATED)
    return report, fetcher


def test_run_checks_each_side_and_counts_every_name():
    report, fetcher = _run()
    assert sorted(fetcher.requested[0][0]) == ["DEEP", "DWN", "GONE", "HTB", "NOSNAP", "OLD", "UPP"]
    assert fetcher.requested[0][2] == AS_OF and fetcher.requested[0][3] == "sip"
    up, down = report.counts[UP], report.counts[DOWN]
    assert (up["entered"], up["structure_pass"], up["structure_fail"]) == (4, 2, 2)
    assert (down["entered"], down["structure_pass"], down["structure_fail"]) == (3, 1, 2)
    assert report.passed == {UP: ["OLD", "UPP"], DOWN: ["DWN"]}
    assert up["reason_counts"][ZONE_TOO_DEEP] == 1 and up["reason_counts"][NO_BARS] == 1
    assert down["reason_counts"][NOT_EASY_TO_BORROW] == 2 and down["reason_counts"][NO_BARS] == 1
    assert report.calls[DOWN]["HTB"]["reasons"] == [NOT_EASY_TO_BORROW]
    assert sum(up["pass_quality_histogram"].values()) == up["structure_pass"]


def test_bars_after_as_of_are_ignored():
    report, _ = _run()
    upp = report.calls[UP]["UPP"]
    assert upp["structure_pass"] and upp["metrics"]["last_bar_date"] == "2026-09-30"


def test_warnings_flag_the_borrow_snapshot_and_a_missing_last_bar():
    report, _ = _run()
    assert any("universe report is not point in time" in w for w in report.warnings)
    assert any("easy_to_borrow" in w and "not point in time" in w for w in report.warnings)
    assert any("no bar on 2026-09-30: OLD" in w for w in report.warnings)


def test_report_round_trips_and_never_overwrites(tmp_path):
    report, _ = _run()
    path = save_report(report, tmp_path)
    assert path.name == "pivot_2026-09-30_20261006T120500Z.json"
    assert load_report(path) == report
    assert json.loads(path.read_text())["policy"]["retrace_zone_pct"] == [35.0, 50.0]
    with pytest.raises(FileExistsError):
        save_report(report, tmp_path)


def test_borrow_flags():
    assert borrow_flags([{"symbol": "A", "easy_to_borrow": True}, {"symbol": "B"}]) == {"A": True, "B": False}


def test_cli_picks_the_newest_trend_report_and_writes(tmp_path, monkeypatch, capsys):
    save_trend(_trend(["UPP"], ["DWN"]), tmp_path)
    snapshot = save_snapshot([{"symbol": "DWN", "easy_to_borrow": True}], tmp_path / "assets", FETCHED_AFTER)
    monkeypatch.setattr(cli, "run_pivot",
                        lambda trend, path, borrow, source, fetched, cache_dir=None: run_pivot(
                            trend, path, borrow, source, fetched, bars_fetcher=FakeBars(FRAMES), created_at=CREATED))
    out = tmp_path / "out"
    code = cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path),
                     "--assets-snapshot", str(snapshot), "--out-dir", str(out)])
    assert code == cli.EXIT_OK
    text = capsys.readouterr().out
    assert "PASS: UPP" in text and "PASS: DWN" in text
    assert len(list(out.glob("pivot_2026-09-30_*.json"))) == 1


def test_cli_bad_input(tmp_path):
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path)]) == cli.EXIT_BAD_INPUT
    save_trend(_trend(["UPP"], []), tmp_path)
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path),
                     "--assets-snapshot", str(tmp_path / "missing.json")]) == cli.EXIT_BAD_INPUT
