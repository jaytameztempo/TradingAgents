"""The read-only backtest: point-in-time files, the regime gate, levels, the bar-by-bar exits and the statistics.

Every report and bar here is made up and written to a temp folder; nothing touches the network.
"""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from extensions.backtest import pit
from extensions.backtest.bars import BarCache
from extensions.backtest.engine import BacktestInputError, run_backtest
from extensions.backtest.levels import Plan, side_plan_from_handoff, trend_plan_for
from extensions.backtest.simulate import Skip, simulate, summarize
from extensions.status.paper_tickets import plan_ticket

pytestmark = pytest.mark.unit

D0 = date(2026, 3, 2)  # a Monday: the as-of of the made-up reports
D1 = date(2026, 3, 3)  # the first session after it
STAMP = "20260302T220000Z"  # 17:00 New York on D0
RULE = "clustered_within_3atr_beyond_line"


# --- made-up files -----------------------------------------------------------------------

def _json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _route(home, label, up=(), down=(), basket=None, stamp=STAMP, as_of=D0):
    _json(home / "routes" / f"route_ALL_{as_of}_{stamp}.json", {
        "as_of": str(as_of), "created_at": "x", "regime_label": label, "regime_symbol": "SPY+QQQ",
        "tradeable": [], "blocked": [], "up_passed": list(up), "down_passed": list(down),
        "basket_file": basket and f"C:\\elsewhere\\baskets\\{basket}", "down_basket_file": None,
        "schema_version": 1})


def _channel(home, members, swing=True, stamp=STAMP, side="long"):
    calls = {}
    for sym in members:
        metrics = {"support": 100.0, "resistance": 110.0, "atr_14": 2.0}
        if swing:
            metrics["stop_swing_low"] = {"price": 98.0, "date": "2026-02-10", "rule": RULE, "window_sessions": 120}
        calls[sym] = {"passed": True, "metrics": metrics}
    name = f"{side}_channel_{D0}_{stamp}.json"
    _json(home / "scanbot" / name, {"as_of": str(D0), "members": list(members), "calls": calls,
                                    "stage_counts": {"at_support": 1, "pivot_confirmed": len(members)}})
    return name


def _side_handoff(home, names, channel_name, stamp=STAMP, support=100.0, as_of=D0):
    entries = {sym: {"side": "long", "setup": "LONG_FADE", "status": "PLAN",
                     "levels": {"support": support, "resistance": 110.0, "midline": 105.0, "atr_14": 2.0,
                                "invalidation": 99.5}} for sym in names}
    _json(home / "handoffs" / f"ranked_side_handoff_{as_of}_{stamp}.json", {
        "as_of": str(as_of), "long_channel_report": f"C:\\elsewhere\\scanbot\\{channel_name}",
        "short_channel_report": None, "names": entries})


def _bars(home, symbol, rows, name_range="2026-01-01_2026-12-31"):
    """rows: (date, open, high, low, close)."""
    frame = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])
    frame.insert(0, "symbol", symbol)
    frame["volume"] = 1000.0
    path = home / "cache" / "bars" / f"{symbol}_{name_range}_sip_split.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def _sessions(start, n):
    return [d.date() for d in pd.bdate_range(start=pd.Timestamp(start), periods=n)]


def _flat_then(after_rows, before=40):
    """``before`` flat sessions ending on D0 (100, 101/99), then ``after_rows`` (open, high, low, close) from D1."""
    rows = [(str(d), 100.0, 101.0, 99.0, 100.0) for d in pd.bdate_range(end=pd.Timestamp(D0), periods=before).date]
    rows += [(str(d), *r) for d, r in zip(_sessions(D1, len(after_rows)), after_rows, strict=True)]
    return rows


# --- point in time -----------------------------------------------------------------------

def test_known_on_is_the_new_york_date():
    assert pit.known_on(pd.Timestamp("2026-03-03T02:00:00Z").to_pydatetime()) == D0  # 21:00 New York on D0
    assert pit.known_on(pd.Timestamp("2026-03-03T14:00:00Z").to_pydatetime()) == D1


def test_a_signal_belongs_to_its_as_of_and_enters_after_the_file_was_written(tmp_path):
    _route(tmp_path, "SIDE")
    ch = _channel(tmp_path, ["AAA"])
    _side_handoff(tmp_path, ["AAA"], ch, stamp="20260305T150000Z")  # written (known) on Mar 5
    _bars(tmp_path, "AAA", _flat_then([(100.5, 102, 99.8, 101)] * 12))
    trade = run_backtest(tmp_path, D0, strategy="side")["trades"][0]
    assert trade["source_file"].startswith("ranked_side_handoff_")
    assert (trade["report_as_of"], trade["run_date"], trade["entry_date"]) == (str(D0), "2026-03-05", "2026-03-06")


def test_a_same_week_rerun_replaces_the_file_and_a_late_one_is_ignored(tmp_path):
    _route(tmp_path, "SIDE")
    ch = _channel(tmp_path, ["AAA"])
    _side_handoff(tmp_path, ["AAA"], ch)
    _side_handoff(tmp_path, ["AAA"], ch, stamp="20260304T150000Z", support=100.4)  # a fix two days later
    _bars(tmp_path, "AAA", _flat_then([(100.5, 102, 99.8, 101)] * 30))
    [trade] = run_backtest(tmp_path, D0, strategy="side")["trades"]
    assert trade["reference_entry"] == 100.4 and trade["run_date"] == "2026-03-04"

    _side_handoff(tmp_path, ["AAA"], ch, stamp="20260320T150000Z", support=100.2)  # 18 days later: hindsight
    [trade] = run_backtest(tmp_path, D0, strategy="side")["trades"]
    assert trade["reference_entry"] == 100.4


def test_a_date_with_no_saved_route_is_not_replayed(tmp_path):
    ch = _channel(tmp_path, ["AAA"])
    _side_handoff(tmp_path, ["AAA"], ch)
    _bars(tmp_path, "AAA", _flat_then([(100.5, 102, 99.8, 101)] * 3))
    result = run_backtest(tmp_path, D0, strategy="side")
    assert result["trades"] == [] and result["skipped"] == []
    assert [d["status"] for d in result["coverage"]] == ["missing"]


# --- regime gate -------------------------------------------------------------------------

def test_upbot_is_blocked_on_a_side_day_and_trades_on_an_up_day(tmp_path):
    _bars(tmp_path, "AAA", _flat_then([(100.0, 101, 99.5, 100.5)] * 12))
    _json(tmp_path / "baskets" / f"scan-up-category-1_{D0}_{STAMP}.json",
          {"as_of": str(D0), "members": [{"ticker": "AAA", "metrics": {"last_swing_low": 95.0, "atr_14": 3.0}}]})
    basket = f"scan-up-category-1_{D0}_{STAMP}.json"
    _route(tmp_path, "SIDE", up=["AAA"], basket=basket)
    blocked = run_backtest(tmp_path, D0, strategy="up")
    assert blocked["trades"] == []
    assert blocked["skipped"][0]["reason"] == "regime SIDE does not match UP"

    _route(tmp_path, "UP", up=["AAA"], basket=basket, stamp="20260302T230000Z")  # the newer route wins
    traded = run_backtest(tmp_path, D0, strategy="up")
    assert [t["symbol"] for t in traded["trades"]] == ["AAA"]


def test_strategy_filter_keeps_other_bots_out(tmp_path):
    _route(tmp_path, "SIDE", up=["BBB"])
    ch = _channel(tmp_path, ["AAA"])
    _side_handoff(tmp_path, ["AAA"], ch)
    _bars(tmp_path, "AAA", _flat_then([(100.5, 102, 99.8, 101)] * 3))
    result = run_backtest(tmp_path, D0, strategy="side")
    assert {s["bot"] for s in result["skipped"]} <= {"side"}
    assert [(row["origin"], row["strategy"]) for row in result["summary"]] == [
        ("replay", "side"), ("replay", "all"), ("reconstructed", "side"), ("reconstructed", "all")]


# --- levels ------------------------------------------------------------------------------

def test_side_levels_are_the_paper_ticket_levels(tmp_path):
    _route(tmp_path, "SIDE")
    ch = _channel(tmp_path, ["AAA"])
    _side_handoff(tmp_path, ["AAA"], ch)
    # D1 opens 100.5; D2 reaches the 105 midline.
    _bars(tmp_path, "AAA", _flat_then([(100.5, 102, 99.8, 101), (101, 105.5, 100.5, 105)]))
    trade = run_backtest(tmp_path, D0, strategy="side")["trades"][0]
    expected = plan_ticket("AAA", "long", 100.0, 2.0, 105.0,
                           {"price": 98.0, "date": "2026-02-10", "rule": RULE})
    assert (trade["stop"], trade["target"], trade["reference_entry"]) == \
           (expected["stop"], expected["target"], expected["entry"]) == (95.99, 105.0, 100.0)
    assert trade["levels_source"] == "recorded"
    assert (trade["entry_date"], trade["entry"]) == (str(D1), 100.5)
    assert (trade["exit_reason"], trade["exit"]) == ("target", 105.0)
    assert trade["r"] == round(4.5 / (100.5 - 95.99), 3)


def test_side_falls_back_to_the_handoff_invalidation_without_a_stop_swing():
    entry = {"levels": {"support": 100.0, "resistance": 110.0, "midline": 105.0, "atr_14": 2.0,
                        "invalidation": 99.5}}
    plan = side_plan_from_handoff("AAA", "long", entry, {"calls": {"AAA": {"metrics": {}}}})
    assert (plan.stop, plan.target, plan.levels_source) == (99.5, 105.0, "recorded")
    assert "invalidation" in plan.detail


def test_trend_levels_from_the_basket():
    daily = pd.DataFrame(columns=["session", "open", "high", "low", "close"])
    plan = trend_plan_for("up", "AAA", "long", {"last_swing_low": 95.0, "atr_14": 2.0}, daily, D0)
    assert (plan.stop, plan.target, plan.target_r, plan.levels_source) == (92.99, None, 2.0, "derived")
    short = trend_plan_for("down", "AAA", "short", {"last_swing_high": 105.0, "atr_14": 2.0}, daily, D0)
    assert short.stop == 107.01


def test_trend_target_is_two_r_from_the_fill(tmp_path):
    _route(tmp_path, "UP", up=["AAA"], basket=f"scan-up-category-1_{D0}_{STAMP}.json")
    _json(tmp_path / "baskets" / f"scan-up-category-1_{D0}_{STAMP}.json",
          {"as_of": str(D0), "members": [{"ticker": "AAA", "metrics": {"last_swing_low": 95.0, "atr_14": 3.0}}]})
    _bars(tmp_path, "AAA", _flat_then([(100.0, 101, 99.5, 100.5)] * 3))
    trade = run_backtest(tmp_path, D0, strategy="up", max_sessions=3)["trades"][0]
    assert trade["stop"] == 91.99 and trade["target"] == round(100.0 + 2 * (100.0 - 91.99), 2)
    assert trade["exit_reason"] == "time" and trade["sessions_held"] == 3


def test_trend_swing_from_bars_ignores_bars_after_the_as_of(tmp_path):
    # Flat at 100, down to a low of 89, back up past 89 x 1.05 (a confirmed swing low) by D0; after D0, a crash.
    closes = [100.0] * 30 + [98, 96, 94, 92, 90, 92, 94, 96, 98, 100]
    days = pd.bdate_range(end=pd.Timestamp(D0), periods=len(closes)).date
    rows = [(str(d), c, c + 1, c - 1, c) for d, c in zip(days, closes, strict=True)]
    rows += [(str(d), 60.0, 61.0, 50.0, 55.0) for d in _sessions(D1, 5)]
    _bars(tmp_path, "AAA", rows)
    daily = BarCache(tmp_path).bars("AAA")
    plan = trend_plan_for("up", "AAA", "long", None, daily, D0)
    assert "swing low 89" in plan.detail
    assert 80 < plan.stop < 89


# --- the simulation ----------------------------------------------------------------------

def _frame(rows, start=D1):
    sessions = _sessions(start, len(rows))
    return pd.DataFrame([(s, pd.Timestamp(s), *r) for s, r in zip(sessions, rows, strict=True)],
                        columns=["session", "time", "open", "high", "low", "close"])


LONG_PLAN = Plan(bot="side", symbol="AAA", side="long", stop=95.0, target=110.0, levels_source="recorded")


def test_stop_and_target_in_one_bar_counts_as_the_stop():
    trade = simulate(LONG_PLAN, _frame([(100, 111, 94, 100)]), D0, 10)
    assert (trade["exit_reason"], trade["exit"], trade["r"]) == ("stop", 95.0, -1.0)


def test_a_gap_through_the_stop_fills_at_the_open():
    trade = simulate(LONG_PLAN, _frame([(100, 101, 99, 100), (90, 91, 89, 90)]), D0, 10)
    assert (trade["exit_reason"], trade["exit"], trade["r"]) == ("stop", 90.0, -2.0)


def test_time_exit_and_open_at_end():
    bars = _frame([(100, 101, 99, 100), (100, 102, 99, 101), (101, 103, 100, 102)])
    timed = simulate(LONG_PLAN, bars, D0, 2)
    assert (timed["exit_reason"], timed["exit"], timed["sessions_held"]) == ("time", 101.0, 2)
    open_end = simulate(LONG_PLAN, bars, D0, 10)
    assert (open_end["exit_reason"], open_end["exit"]) == ("open_at_end", 102.0)


def test_an_open_already_past_a_level_is_not_traded():
    with pytest.raises(Skip, match="past the stop"):
        simulate(LONG_PLAN, _frame([(94, 96, 93, 95)]), D0, 10)
    with pytest.raises(Skip, match="past the target"):
        simulate(LONG_PLAN, _frame([(111, 112, 110, 111)]), D0, 10)
    with pytest.raises(Skip, match="no cached bars"):
        simulate(LONG_PLAN, _frame([(100, 101, 99, 100)], start=date(2026, 2, 2)), D0, 10)


def test_a_short_reaches_its_target():
    plan = Plan(bot="down", symbol="AAA", side="short", stop=105.0, target=None, levels_source="derived",
                target_r=2.0, max_risk=100.0)
    trade = simulate(plan, _frame([(100, 101, 98, 99), (99, 99.5, 89, 90)]), D0, 10)
    assert (trade["target"], trade["exit_reason"], trade["exit"], trade["r"]) == (90.0, "target", 90.0, 2.0)


def test_risk_over_three_atr_is_skipped():
    plan = Plan(bot="up", symbol="AAA", side="long", stop=80.0, target=None, levels_source="derived",
                target_r=2.0, max_risk=6.0)
    with pytest.raises(Skip, match="over 3 x ATR"):
        simulate(plan, _frame([(100, 101, 99, 100)]), D0, 10)


def test_four_hour_bars_enter_on_the_first_bar_of_the_next_session(tmp_path):
    _route(tmp_path, "SIDE")
    ch = _channel(tmp_path, ["AAA"])
    _side_handoff(tmp_path, ["AAA"], ch)
    rows = [("AAA", f"{D0} 13:30:00", str(D0), 100, 101, 99, 100, 1, 5),
            ("AAA", f"{D1} 09:30:00", str(D1), 100.5, 101, 100, 100.8, 1, 8),
            ("AAA", f"{D1} 13:30:00", str(D1), 100.8, 105.2, 100.6, 105, 1, 5)]
    path = tmp_path / "cache" / "bars" / "AAA_2026-01-01_2026-12-31_sip_split.4h_rth.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=["symbol", "timestamp", "date", "open", "high", "low", "close", "volume",
                                "source_bars"]).to_csv(path, index=False)
    trade = run_backtest(tmp_path, D0, strategy="side", timeframe="4h")["trades"][0]
    assert (trade["entry"], trade["exit_reason"], trade["sessions_held"]) == (100.5, "target", 1)


def test_one_position_a_symbol_a_bot(tmp_path):
    _route(tmp_path, "SIDE")
    ch = _channel(tmp_path, ["AAA"])
    _side_handoff(tmp_path, ["AAA"], ch)
    _route(tmp_path, "SIDE", as_of=D1, stamp="20260303T220000Z")
    _side_handoff(tmp_path, ["AAA"], ch, stamp="20260303T220000Z", as_of=D1)  # next day's handoff, still holding
    _bars(tmp_path, "AAA", _flat_then([(100.5, 102, 99.8, 101)] * 12))
    result = run_backtest(tmp_path, D0, D1, strategy="side")
    assert len(result["trades"]) == 1
    assert any(s["reason"].startswith("already holding AAA") for s in result["skipped"])


# --- bars and statistics -----------------------------------------------------------------

def test_overlapping_bar_files_merge_with_the_later_range_winning(tmp_path):
    _bars(tmp_path, "AAA", [("2026-03-02", 1, 2, 0.5, 1.5), ("2026-03-03", 1, 2, 0.5, 1.5)],
          name_range="2025-01-01_2026-03-03")
    _bars(tmp_path, "AAA", [("2026-03-03", 9, 9, 9, 9), ("2026-03-04", 3, 4, 2, 3)],
          name_range="2025-02-01_2026-03-04")
    _bars(tmp_path, "AAAB", [("2026-03-05", 7, 7, 7, 7)])  # another symbol sharing the prefix is not read
    bars = BarCache(tmp_path).bars("AAA")
    assert [str(s) for s in bars["session"]] == ["2026-03-02", "2026-03-03", "2026-03-04"]
    assert bars["close"].tolist() == [1.5, 9.0, 3.0]


def test_summary_and_curve():
    def t(bot, r, exit_date, reason="target", origin="replay"):
        return {"bot": bot, "r": r, "exit_date": exit_date, "entry_date": "2026-03-03", "symbol": bot + exit_date,
                "exit_reason": reason, "origin": origin}
    trades = [t("side", 2.0, "2026-03-04"), t("side", -1.0, "2026-03-05"), t("up", 0.5, "2026-03-06"),
              t("up", 9.0, "2026-03-07", "open_at_end"), t("side", 3.0, "2026-03-05", origin="reconstructed")]
    rows, curve = summarize(trades, 1.0, D0, origins=("replay", "reconstructed"))
    by = {(r["origin"], r["strategy"]): r for r in rows}
    every = by[("replay", "all")]
    assert (every["trades"], every["open"], every["wins"], every["win_rate"], every["avg_r"]) == (3, 1, 2, 66.67, 0.5)
    assert every["total_return_pct"] == 1.5
    assert every["max_drawdown_pct"] == round(1 / 102 * 100, 3)
    assert by[("reconstructed", "all")]["total_r"] == 3.0  # never added to the replay figures
    assert [p["replay"] for p in curve] == [100.0, 102.0, 101.0, 101.5]
    assert [p["reconstructed"] for p in curve] == [100.0, 100.0, 103.0, 103.0]


def test_bad_inputs_are_refused(tmp_path):
    with pytest.raises(BacktestInputError):
        run_backtest(tmp_path, D1, D0)
    with pytest.raises(BacktestInputError):
        run_backtest(tmp_path, D0, strategy="moon")
    with pytest.raises(BacktestInputError):
        run_backtest(tmp_path, D0, timeframe="1m")


def test_the_real_data_folder_is_never_written(tmp_path):
    _route(tmp_path, "SIDE")
    ch = _channel(tmp_path, ["AAA"])
    _side_handoff(tmp_path, ["AAA"], ch)
    _bars(tmp_path, "AAA", _flat_then([(100.5, 102, 99.8, 101)] * 3))
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    run_backtest(tmp_path, D0, date(2026, 3, 10))
    assert {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == before


# --- hold until stop or target --------------------------------------------------------------------

def test_holding_runs_past_max_sessions_and_exits_at_the_target():
    bars = _frame([(100, 101, 99, 100)] * 14 + [(100, 111, 99.5, 110)])  # the target is reached on session 15
    assert simulate(LONG_PLAN, bars, D0, 10)["exit_reason"] == "time"
    held = simulate(LONG_PLAN, bars, D0, None)
    assert (held["exit_reason"], held["exit"], held["sessions_held"]) == ("target", 110.0, 15)


def test_holding_a_trade_that_reaches_neither_level_is_open_at_the_end():
    held = simulate(LONG_PLAN, _frame([(100, 101, 99, 100)] * 30), D0, None)
    assert (held["exit_reason"], held["sessions_held"]) == ("open_at_end", 30)


def test_a_held_open_trade_keeps_its_symbol_held(tmp_path):
    _route(tmp_path, "SIDE")
    ch = _channel(tmp_path, ["AAA"])
    _side_handoff(tmp_path, ["AAA"], ch)
    _route(tmp_path, "SIDE", as_of=date(2026, 3, 16), stamp="20260316T220000Z")
    _side_handoff(tmp_path, ["AAA"], ch, stamp="20260316T220000Z", as_of=date(2026, 3, 16))
    _bars(tmp_path, "AAA", _flat_then([(100.5, 102, 99.8, 101)] * 30))  # never reaches 105 or the stop
    result = run_backtest(tmp_path, D0, date(2026, 3, 16), strategy="side", hold_to_exit=True)
    assert [t["exit_reason"] for t in result["trades"]] == ["open_at_end"]
    assert any(s["reason"].startswith("already holding AAA") for s in result["skipped"])
    assert result["params"]["hold_to_exit"] is True
    # with the default 10-session limit the first trade closes on time and the second one trades
    limited = run_backtest(tmp_path, D0, date(2026, 3, 16), strategy="side")
    assert [t["exit_reason"] for t in limited["trades"]] == ["time", "time"]
