"""Side 4-hour confirmation checks only the ranked handoff's planned names: a long passes if the last
regular-session 4-hour close is within 0.6 ATR above support and no more than 0.25 ATR through it; a
short is the mirror at resistance. The last bar must be on as_of, bars after as_of are ignored, plans
are not rewritten and nothing is overwritten. No network.
"""

import json
from datetime import UTC, date, datetime

import pandas as pd
import pytest

from extensions.router.strategy_router import MissingInput
from extensions.scanbot.funnel import CalendarError
from extensions.scanbot.side_confirm_4h import (
    BROKEN,
    LONG,
    MISSING_LEVELS,
    NO_4H_BARS,
    SHORT,
    STALE_LAST_BAR,
    TOO_FAR,
    confirm_side_4h,
    latest_ranked_side_handoff,
    load_report,
    run_side_confirm_4h,
    save_report,
)
from extensions.scripts import run_side_confirm_4h as cli

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
LEVELS = {"support": 100.0, "resistance": 110.0, "atr_14": 2.0}


def _bars(symbol, closes, day=AS_OF):
    """Two 4-hour bars on ``day``; the second closes at closes[-1]."""
    rows = []
    for i, close in enumerate(closes):
        ts = pd.Timestamp(day) + pd.Timedelta(hours=9 + 4 * i, minutes=30)
        rows.append({"symbol": symbol, "timestamp": ts, "date": pd.Timestamp(day), "open": close,
                     "high": close + 1, "low": close - 1, "close": close, "volume": 1000})
    return pd.DataFrame(rows)


def _handoff(long_names=("AAA",), short_names=("ZZZ",), levels=LEVELS):
    names = {s: {"setup": "LONG_FADE", "status": "PLAN", "levels": dict(levels)} for s in long_names}
    names.update({s: {"setup": "SHORT_FADE", "status": "PLAN", "levels": dict(levels)} for s in short_names})
    names["DROP"] = {"setup": "LONG_FADE", "status": "NOT_RANKED", "plan_file": None}
    return {"as_of": AS_OF.isoformat(), "created_at": "2026-10-08T11:04:09+00:00", "regime_label": "SIDE",
            "planned_long": list(long_names), "planned_short": list(short_names), "not_ranked": ["DROP"],
            "names": names, "warnings": ["w1"]}


@pytest.mark.parametrize("close, passed, code", [
    (100.0, True, None),      # on support
    (101.18, True, None),     # +0.59 ATR, inside the edge
    (101.3, False, TOO_FAR),  # +0.65 ATR
    (99.5, True, None),       # -0.25 ATR, at the edge
    (99.4, False, BROKEN),    # -0.30 ATR
])
def test_long_near_and_not_broken(close, passed, code):
    call = confirm_side_4h(LONG, _bars("AAA", [105.0, close]), LEVELS, AS_OF)
    assert call.confirm_pass is passed
    assert call.reasons == ([] if passed else [code])
    assert call.metrics["last_4h_close"] == close


@pytest.mark.parametrize("close, passed, code", [
    (110.0, True, None),
    (108.82, True, None),     # 0.59 ATR below resistance
    (108.7, False, TOO_FAR),
    (110.5, True, None),      # 0.25 ATR through
    (110.6, False, BROKEN),
])
def test_short_near_and_not_broken(close, passed, code):
    call = confirm_side_4h(SHORT, _bars("ZZZ", [105.0, close]), LEVELS, AS_OF)
    assert call.confirm_pass is passed
    assert call.reasons == ([] if passed else [code])


def test_only_the_last_bar_counts():
    # The first bar broke support, the last closed back near it.
    assert confirm_side_4h(LONG, _bars("AAA", [95.0, 100.5]), LEVELS, AS_OF).confirm_pass


def test_missing_data_fails():
    empty = _bars("AAA", [100.0]).iloc[0:0]
    assert confirm_side_4h(LONG, empty, LEVELS, AS_OF).reasons == [NO_4H_BARS]
    stale = confirm_side_4h(LONG, _bars("AAA", [100.0, 100.0], day=date(2026, 9, 29)), LEVELS, AS_OF)
    assert stale.reasons == [STALE_LAST_BAR]
    no_atr = confirm_side_4h(LONG, _bars("AAA", [100.0, 100.0]), {"support": 100.0}, AS_OF)
    assert no_atr.reasons == [MISSING_LEVELS]


def _fetcher(frames, seen=None):
    def fetch(symbols, start, end, **kwargs):
        if seen is not None:
            seen.append((list(symbols), start, end, kwargs))
        return pd.concat(frames, ignore_index=True)
    return fetch


def test_run_checks_planned_names_only_and_ignores_bars_after_as_of(tmp_path):
    later = _bars("AAA", [90.0, 90.0], day=date(2026, 10, 1))  # a break after as_of must not count
    seen = []
    frames = [_bars("SPY", [500.0, 500.0]), _bars("AAA", [100.0, 100.4]), later, _bars("ZZZ", [111.0, 111.0])]
    report = run_side_confirm_4h(_handoff(), tmp_path / "h.json", bars_fetcher=_fetcher(frames, seen),
                                 created_at=CREATED)
    symbols, start, end, kwargs = seen[0]
    assert symbols == ["SPY", "AAA", "ZZZ"] and end == AS_OF and start < AS_OF and kwargs["feed"] == "sip"
    assert report.confirmed == {LONG: ["AAA"], SHORT: []}
    assert report.failed == {LONG: [], SHORT: ["ZZZ"]}
    assert report.calls[SHORT]["ZZZ"]["reasons"] == [BROKEN]
    assert "DROP" not in report.calls[LONG]
    assert report.counts["total"] == {"planned": 2, "checked_4h": 2, "pass_4h": 1, "fail_4h": 1}
    assert report.counts[SHORT]["reason_counts"][BROKEN] == 1
    assert report.plans_rewritten is False and report.orders_placed is False
    assert report.warnings == ["w1"]


def test_run_needs_the_calendar_symbol(tmp_path):
    with pytest.raises(CalendarError):
        run_side_confirm_4h(_handoff(), "h.json", bars_fetcher=_fetcher([_bars("AAA", [100.0, 100.0])]))


def test_run_refuses_a_planned_name_without_a_plan_entry():
    handoff = _handoff()
    handoff["names"]["AAA"]["status"] = "NOT_RANKED"
    with pytest.raises(ValueError, match="AAA"):
        run_side_confirm_4h(handoff, "h.json", bars_fetcher=_fetcher([]))


def test_save_never_overwrites_and_round_trips(tmp_path):
    frames = [_bars("SPY", [500.0, 500.0]), _bars("AAA", [100.0, 100.0]), _bars("ZZZ", [110.0, 110.0])]
    report = run_side_confirm_4h(_handoff(), "h.json", bars_fetcher=_fetcher(frames), created_at=CREATED)
    path = save_report(report, tmp_path)
    assert path.name == "side_confirm4h_2026-09-30_20261008T120000Z.json"
    assert load_report(path) == report
    with pytest.raises(FileExistsError):
        save_report(report, tmp_path)


def test_latest_handoff_is_picked_by_created_at(tmp_path):
    old, new = _handoff(), _handoff(long_names=("BBB",))
    new["created_at"] = "2026-10-08T12:00:00+00:00"
    (tmp_path / "ranked_side_handoff_2026-09-30_b.json").write_text(json.dumps(old), encoding="utf-8")
    (tmp_path / "ranked_side_handoff_2026-09-30_a.json").write_text(json.dumps(new), encoding="utf-8")
    path, data = latest_ranked_side_handoff(AS_OF, tmp_path)
    assert path.name.endswith("_a.json") and data["planned_long"] == ["BBB"]
    with pytest.raises(MissingInput):
        latest_ranked_side_handoff(date(2026, 9, 29), tmp_path)


def test_cli_writes_the_report(tmp_path, monkeypatch, capsys):
    handoff_path = tmp_path / "ranked_side_handoff_2026-09-30_x.json"
    handoff_path.write_text(json.dumps(_handoff()), encoding="utf-8")
    frames = [_bars("SPY", [500.0, 500.0]), _bars("AAA", [100.0, 100.0]), _bars("ZZZ", [110.0, 110.0])]
    monkeypatch.setattr("extensions.scanbot.side_confirm_4h.fetch_4hour_bars", _fetcher(frames))
    monkeypatch.setattr(cli, "run_side_confirm_4h", lambda h, p, **kw: run_side_confirm_4h(
        h, p, bars_fetcher=_fetcher(frames), **kw))
    out_dir = tmp_path / "out"
    assert cli.main(["--handoff", str(handoff_path), "--out-dir", str(out_dir)]) == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "total: checked 2, pass 2, fail 0" in printed and "no order placed" in printed
    assert len(list(out_dir.glob("side_confirm4h_2026-09-30_*.json"))) == 1
