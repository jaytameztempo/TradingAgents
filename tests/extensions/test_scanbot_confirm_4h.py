"""SCANBot 4-hour confirmation checks only the daily pivot passes, on regular-session 4-hour bars built
from 30-minute bars (no extended hours, no hourly bars): a bar on every session since the daily swing,
the 4-hour extreme on the swing date, and the last 4-hour close past the extreme bar's far side. Daily
fails are carried, never re-checked. Bars after as_of are ignored and nothing is overwritten. No network.
"""

import json
from datetime import UTC, date, datetime

import pandas as pd
import pytest
from alpaca.data.timeframe import TimeFrameUnit

from extensions.market_data.alpaca_bars import FOUR_HOUR_COLUMNS, fetch_4hour_bars, regular_session_4hour
from extensions.scanbot.confirm_4h import (
    MISSING_SESSIONS,
    NEW_EXTREME,
    NO_4H_BARS,
    NO_DAILY_SWING,
    NOT_PAST_EXTREME_BAR,
    confirm_4h,
    load_report,
    run_confirm_4h,
    save_report,
)
from extensions.scanbot.funnel import CalendarError
from extensions.scanbot.pivot import DOWN, UP, PivotReport
from extensions.scanbot.pivot import save_report as save_pivot
from extensions.scripts import run_scanbot_confirm_4h as cli

AS_OF = date(2026, 9, 30)
SWING = date(2026, 9, 21)
CREATED = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
SESSIONS = [d.date() for d in pd.bdate_range(SWING, AS_OF)]  # 8 sessions, no holidays


def _bars(symbol, rows):
    """rows: (date, high, low, close) per 4-hour bar, two per session in order."""
    out, seen = [], {}
    for day, high, low, close in rows:
        day = pd.Timestamp(day)
        k = seen[day] = seen.get(day, -1) + 1
        start = day + (pd.Timedelta(hours=9, minutes=30) if k == 0 else pd.Timedelta(hours=13, minutes=30))
        out.append({"symbol": symbol, "timestamp": start, "date": day, "open": close, "high": high,
                    "low": low, "close": close, "volume": 1000.0, "source_bars": 8 if k == 0 else 5})
    frame = pd.DataFrame(out).reindex(columns=FOUR_HOUR_COLUMNS)
    for column in ("timestamp", "date"):
        frame[column] = pd.to_datetime(frame[column])
    return frame


def _up_rows(peak=120.0, step=0.5, last_close=110.0):
    """Peak bar first on the swing date (high ``peak``, low 3 under it), then highs stepping down every bar."""
    rows, level = [(SESSIONS[0], peak, peak - 3, peak - 1)], peak
    for d in [SESSIONS[0], *(s for s in SESSIONS[1:] for _ in range(2))]:
        level -= step
        rows.append((d, level, level - 2, level - 1))
    d, high, low, _ = rows[-1]
    rows[-1] = (d, high, min(low, last_close), last_close)
    return rows


UP_ROWS = _up_rows()  # 16 bars: peak 120 / low 117 on 9-21, last close 110 on 9-30 13:30


def _mirror(rows):
    return [(d, 200 - low, 200 - high, 200 - close) for d, high, low, close in rows]


DOWN_ROWS = _mirror(UP_ROWS)  # trough 80 / high 83 on 9-21, last close 90


# --- the rules ---------------------------------------------------------------

def test_up_and_down_pass():
    up = confirm_4h(UP, _bars("A", UP_ROWS), SWING, SESSIONS)
    assert up.confirm_pass and up.reasons == []
    assert up.metrics["extreme_4h_high"] == 120.0 and up.metrics["extreme_bar_low"] == 117.0
    assert up.metrics["last_4h_close"] == 110.0 and up.metrics["missing_sessions"] == []
    down = confirm_4h(DOWN, _bars("A", DOWN_ROWS), SWING, SESSIONS)
    assert down.confirm_pass and down.metrics["extreme_4h_low"] == 80.0 and down.metrics["extreme_bar_high"] == 83.0


def test_a_higher_high_after_the_swing_date_fails():
    rows = list(UP_ROWS)
    d, _, low, close = rows[6]
    rows[6] = (d, 120.5, low, close)  # 9-24: a new 4-hour high
    assert confirm_4h(UP, _bars("A", rows), SWING, SESSIONS).reasons == [NEW_EXTREME]
    rows[6] = (d, 120.0, low, close)  # only equal: the first bar at the extreme is still on the swing date
    assert confirm_4h(UP, _bars("A", rows), SWING, SESSIONS).confirm_pass
    down = list(DOWN_ROWS)
    d, high, _, close = down[6]
    down[6] = (d, high, 79.5, close)
    assert confirm_4h(DOWN, _bars("A", down), SWING, SESSIONS).reasons == [NEW_EXTREME]


def test_last_close_must_be_strictly_past_the_extreme_bar():
    rows = list(UP_ROWS)
    d, high, low, _ = rows[-1]
    rows[-1] = (d, 118.0, low, 117.0)  # equal to the peak bar's low
    assert confirm_4h(UP, _bars("A", rows), SWING, SESSIONS).reasons == [NOT_PAST_EXTREME_BAR]
    rows[-1] = (d, 118.0, low, 116.99)
    assert confirm_4h(UP, _bars("A", rows), SWING, SESSIONS).confirm_pass
    down = _mirror(rows[:-1] + [(d, 118.0, low, 117.0)])
    assert confirm_4h(DOWN, _bars("A", down), SWING, SESSIONS).reasons == [NOT_PAST_EXTREME_BAR]


def test_peak_on_the_last_bar_cannot_pass():
    rows = UP_ROWS[:-1] + [(AS_OF, 125.0, 121.0, 124.0)]
    assert confirm_4h(UP, _bars("A", rows), SWING, SESSIONS).reasons == [NEW_EXTREME, NOT_PAST_EXTREME_BAR]


def test_a_missing_session_fails_and_the_other_rules_still_run():
    gap = date(2026, 9, 24)
    rows = [r for r in UP_ROWS if r[0] != gap]
    call = confirm_4h(UP, _bars("A", rows), SWING, SESSIONS)
    assert call.reasons == [MISSING_SESSIONS] and call.metrics["missing_sessions"] == ["2026-09-24"]
    stale = [r for r in UP_ROWS if r[0] != AS_OF]
    assert confirm_4h(UP, _bars("A", stale), SWING, SESSIONS).reasons == [MISSING_SESSIONS]


def test_no_bars_and_no_swing():
    assert confirm_4h(UP, _bars("A", []), SWING, SESSIONS).reasons == [NO_4H_BARS]
    before = [(date(2026, 9, 18), 130.0, 120.0, 125.0)]
    assert confirm_4h(UP, _bars("A", before), SWING, SESSIONS).reasons == [NO_4H_BARS]
    assert confirm_4h(DOWN, _bars("A", DOWN_ROWS), None, SESSIONS).reasons == [NO_DAILY_SWING]
    with pytest.raises(ValueError):
        confirm_4h("SIDE", _bars("A", UP_ROWS), SWING, SESSIONS)


# --- regular-session 4-hour bars ---------------------------------------------

class _Bars:
    def __init__(self, df):
        self.df = df


class FakeClient:
    """Answers with 30-minute bars on 2026-06-01, including pre-market and after-hours ones."""

    STARTS = {  # New York start time: (high, low, close, volume)
        "08:00": (150.0, 90.0, 120.0, 10.0),  # pre-market
        "09:00": (149.0, 91.0, 119.0, 10.0),  # pre-market, ends at the open
        "09:30": (101.0, 99.0, 100.5, 100.0),
        "13:00": (103.0, 100.0, 102.0, 100.0),
        "13:30": (104.0, 101.0, 103.0, 50.0),
        "15:30": (105.0, 102.0, 104.5, 50.0),
        "16:00": (160.0, 80.0, 130.0, 10.0),  # after-hours
        "19:30": (161.0, 79.0, 131.0, 10.0),
    }

    def __init__(self):
        self.requests = []

    def get_stock_bars(self, request):
        self.requests.append(request)
        stamps = pd.DatetimeIndex([pd.Timestamp(f"2026-06-01 {t}", tz="America/New_York") for t in self.STARTS])
        index = pd.MultiIndex.from_product([request.symbol_or_symbols, stamps.tz_convert("UTC")],
                                           names=["symbol", "timestamp"])
        values = list(self.STARTS.values()) * len(request.symbol_or_symbols)
        return _Bars(pd.DataFrame({
            "open": [v[2] for v in values], "high": [v[0] for v in values], "low": [v[1] for v in values],
            "close": [v[2] for v in values], "volume": [v[3] for v in values],
        }, index=index))


def test_4hour_bars_are_regular_session_only_from_30_minute_bars():
    client = FakeClient()
    bars = fetch_4hour_bars(["spy"], "2026-06-01", "2026-06-01", client=client)
    (request,) = client.requests
    assert request.timeframe.amount_value == 30 and request.timeframe.unit_value == TimeFrameUnit.Minute
    assert list(bars.columns) == FOUR_HOUR_COLUMNS
    assert bars["timestamp"].dt.strftime("%H:%M").tolist() == ["09:30", "13:30"]
    morning, afternoon = bars.iloc[0], bars.iloc[1]
    # Pre-market and after-hours extremes never reach the 4-hour bars.
    assert (morning["high"], morning["low"], morning["close"], morning["volume"], morning["source_bars"]) == (
        103.0, 99.0, 102.0, 200.0, 2)
    assert (afternoon["high"], afternoon["low"], afternoon["close"], afternoon["source_bars"]) == (105.0, 101.0, 104.5, 2)
    assert (bars["date"] == pd.Timestamp("2026-06-01")).all()


def test_4hour_bars_cache_apart_from_daily_bars(tmp_path):
    client = FakeClient()
    first = fetch_4hour_bars("SPY", "2026-06-01", "2026-06-01", client=client, cache_dir=tmp_path)
    second = fetch_4hour_bars("SPY", "2026-06-01", "2026-06-01", client=client, cache_dir=tmp_path)
    assert len(client.requests) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["SPY_2026-06-01_2026-06-01_iex_split.4h_rth.csv"]
    pd.testing.assert_frame_equal(second, first, check_dtype=False)


def test_regular_session_4hour_of_nothing_is_empty():
    assert regular_session_4hour(pd.DataFrame()).empty


# --- the run -----------------------------------------------------------------

def _call(swing_date, passed=True):
    metrics = {"impulse_end": {"kind": "HIGH", "date": swing_date.isoformat(), "price": 120.0}} if swing_date else {}
    return {"side": UP, "structure_pass": passed, "reasons": [] if passed else ["retrace_over_50pct"],
            "detail": [], "quality": {}, "quality_passed": 0, "metrics": metrics}


def _pivot(passed=None, calls=None):
    calls = calls or {
        UP: {"UPP": _call(SWING), "LATE": _call(SWING), "DEEP": _call(SWING, passed=False)},
        DOWN: {"DWN": _call(SWING), "DFAIL": _call(None, passed=False)},
    }
    passed = passed or {UP: ["LATE", "UPP"], DOWN: ["DWN"]}
    return PivotReport(
        as_of=AS_OF, created_at=CREATED, trend_report="trend.json", trend_created_at=CREATED.isoformat(),
        assets_source="assets.json", assets_fetched_at=CREATED.isoformat(), policy={}, counts={},
        passed=passed, calls=calls, warnings=["easy_to_borrow ... not point in time"],
    )


LATE_ROWS = list(UP_ROWS)
LATE_ROWS[10] = (LATE_ROWS[10][0], 122.0, LATE_ROWS[10][2], LATE_ROWS[10][3])  # higher high on 9-28
FRAMES = {
    "SPY": _bars("SPY", UP_ROWS),
    # A later 4-hour bar would make a new high; it must be ignored.
    "UPP": pd.concat([_bars("UPP", UP_ROWS), _bars("UPP", [(date(2026, 10, 1), 140.0, 111.0, 139.0)])],
                     ignore_index=True),
    "LATE": _bars("LATE", LATE_ROWS),
    "DWN": _bars("DWN", DOWN_ROWS),
    "DEEP": _bars("DEEP", UP_ROWS),
}


class FakeFetcher:
    def __init__(self, frames=FRAMES):
        self.frames = frames
        self.requested = []

    def __call__(self, symbols, start, end, *, feed, cache_dir=None):
        self.requested.append((list(symbols), start, end, feed))
        frames = [self.frames[s] for s in symbols if s in self.frames]
        return pd.concat(frames, ignore_index=True) if frames else _bars("X", [])


def _run(pivot=None, fetcher=None):
    fetcher = fetcher or FakeFetcher()
    return run_confirm_4h(pivot or _pivot(), "pivot.json", bars_fetcher=fetcher, created_at=CREATED), fetcher


def test_run_checks_only_daily_passes_and_carries_fails():
    report, fetcher = _run()
    (symbols, start, end, feed), = fetcher.requested
    assert symbols == ["SPY", "LATE", "UPP", "DWN"] and start == SWING and end == AS_OF and feed == "sip"
    assert set(report.calls[UP]) == {"LATE", "UPP"} and set(report.calls[DOWN]) == {"DWN"}
    assert report.daily_fails == {UP: ["DEEP"], DOWN: ["DFAIL"]}
    assert report.confirmed == {UP: ["UPP"], DOWN: ["DWN"]}
    up, down = report.counts[UP], report.counts[DOWN]
    assert (up["daily_entered"], up["daily_pass"], up["daily_fail_carried"]) == (3, 2, 1)
    assert (up["checked_4h"], up["pass_4h"], up["fail_4h"]) == (2, 1, 1)
    assert (up["final_pass"], up["final_fail"]) == (1, 2)
    assert up["reason_counts"][NEW_EXTREME] == 1
    assert (down["checked_4h"], down["pass_4h"], down["fail_4h"], down["final_fail"]) == (1, 1, 0, 1)
    assert report.warnings == ["easy_to_borrow ... not point in time"]


def test_bars_after_as_of_are_ignored():
    report, _ = _run()
    upp = report.calls[UP]["UPP"]
    assert upp["confirm_pass"] and upp["metrics"]["last_4h_bar"].startswith("2026-09-30T13:30")


def test_a_pass_without_a_passing_daily_call_is_refused():
    with pytest.raises(ValueError, match="without a passing call"):
        _run(_pivot(passed={UP: ["DEEP"], DOWN: []}))


def test_no_calendar_bars_raises():
    frames = {k: v for k, v in FRAMES.items() if k != "SPY"}
    with pytest.raises(CalendarError):
        _run(fetcher=FakeFetcher(frames))


def test_no_daily_passes_fetches_nothing():
    calls = {UP: {"DEEP": _call(SWING, passed=False)}, DOWN: {}}
    pivot = _pivot(passed={UP: [], DOWN: []}, calls=calls)
    report, fetcher = _run(pivot)
    assert fetcher.requested == []
    assert report.confirmed == {UP: [], DOWN: []} and report.counts[UP]["final_fail"] == 1


def test_report_round_trips_and_never_overwrites(tmp_path):
    report, _ = _run()
    path = save_report(report, tmp_path)
    assert path.name == "confirm4h_2026-09-30_20261007T120000Z.json"
    assert load_report(path) == report
    policy = json.loads(path.read_text())["policy"]
    assert policy["extended_hours"] is False and policy["hourly_bars_used"] is False
    with pytest.raises(FileExistsError):
        save_report(report, tmp_path)


def test_cli_picks_the_newest_pivot_report_and_writes(tmp_path, monkeypatch, capsys):
    save_pivot(_pivot(), tmp_path)
    monkeypatch.setattr(cli, "run_confirm_4h", lambda pivot, path, cache_dir=None: run_confirm_4h(
        pivot, path, bars_fetcher=FakeFetcher(), created_at=CREATED))
    out = tmp_path / "out"
    code = cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path), "--out-dir", str(out)])
    assert code == cli.EXIT_OK
    text = capsys.readouterr().out
    assert "CONFIRMED: UPP" in text and "CONFIRMED: DWN" in text and "LATE: FAIL" in text
    assert "4-hour pass 1, fail 1" in text
    assert len(list(out.glob("confirm4h_2026-09-30_*.json"))) == 1


def test_cli_bad_input(tmp_path):
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path)]) == cli.EXIT_BAD_INPUT
    assert cli.main(["--as-of", "30-09-2026"]) == cli.EXIT_BAD_INPUT
    assert cli.main(["--pivot-report", str(tmp_path / "missing.json")]) == cli.EXIT_BAD_INPUT
