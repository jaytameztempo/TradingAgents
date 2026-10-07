"""SCANBot step 3 labels STRONG survivors UP, DOWN or UNCLASSIFIED with RegimeBot's cutoffs,
leaves a name with tangled averages unclassified even when aligned with ADX >= 25, records every
reason, ignores bars after as_of, counts every name once, and never overwrites. No network.
"""

import json
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from extensions.scanbot.financials import STRONG, WEAK, FinancialsReport, GatePass
from extensions.scanbot.financials import save_report as save_financials
from extensions.scanbot.trend import (
    ADX_BELOW_MIN,
    DOWN,
    NO_BARS,
    NOT_ALIGNED,
    SHORT_HISTORY,
    SMA_CROSSED,
    SMA_WITHIN_SPREAD,
    UNCLASSIFIED,
    UP,
    classify_trend,
    decide_trend,
    load_report,
    run_trend,
    save_report,
)
from extensions.scripts import run_scanbot_trend as cli

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 10, 6, 12, 5, tzinfo=UTC)

RISING = [100 + 0.5 * i for i in range(260)]
FALLING = [300 - 0.5 * i for i in range(260)]
CHOP = [100 + (0.5 if i % 2 else -0.5) for i in range(260)]
# Down for 220 sessions then a steep rally: aligned up with ADX near 95, but the
# 50-day crossed above the 200-day inside the last 20 sessions.
V_TURN = [200 - 0.5 * i for i in range(220)] + [90 + 2 * i for i in range(55)]


def _frame(symbol, closes, end=AS_OF):
    closes = np.array(closes, dtype=float)
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    return pd.DataFrame({
        "symbol": symbol, "date": dates, "open": closes, "high": closes + 0.3,
        "low": closes - 0.3, "close": closes, "volume": 1_000_000,
    })


def _decide(**overrides):
    numbers = dict(close=110.0, sma_fast=105.0, sma_slow=100.0, adx=30.0, spread_pct=5.0, crossed=False)
    numbers.update(overrides)
    return decide_trend(**numbers)


# --- the rules ---------------------------------------------------------------

def test_aligned_and_trending_is_up_or_down():
    assert _decide()[:2] == (UP, [])
    assert _decide(close=90.0, sma_fast=95.0, sma_slow=100.0, spread_pct=-5.0)[:2] == (DOWN, [])


def test_adx_exactly_25_counts_and_just_under_does_not():
    assert _decide(adx=25.0)[0] == UP
    assert _decide(adx=24.99)[:2] == (UNCLASSIFIED, [ADX_BELOW_MIN])


def test_not_aligned_is_unclassified():
    assert _decide(close=104.0)[:2] == (UNCLASSIFIED, [NOT_ALIGNED])
    assert _decide(close=104.0, adx=21.0)[1] == [NOT_ALIGNED, ADX_BELOW_MIN]


def test_tangled_averages_block_an_aligned_trending_name():
    label, codes, detail = _decide(spread_pct=0.6, sma_fast=100.6)
    assert (label, codes) == (UNCLASSIFIED, [SMA_WITHIN_SPREAD])
    assert "within 1%" in detail[0]
    assert _decide(crossed=True)[:2] == (UNCLASSIFIED, [SMA_CROSSED])
    assert _decide(close=90.0, sma_fast=95.0, sma_slow=100.0, spread_pct=-5.0, crossed=True)[:2] == (
        UNCLASSIFIED, [SMA_CROSSED])


def test_spread_of_exactly_1pct_is_not_tangled():
    assert _decide(spread_pct=1.0)[0] == UP


# --- bars --------------------------------------------------------------------

def test_classify_trend_on_bars():
    assert classify_trend(_frame("A", RISING)).label == UP
    assert classify_trend(_frame("A", FALLING)).label == DOWN
    chop = classify_trend(_frame("A", CHOP))
    assert chop.label == UNCLASSIFIED and SMA_WITHIN_SPREAD in chop.reasons and NOT_ALIGNED in chop.reasons


def test_recent_cross_on_real_bars_is_unclassified_with_only_that_reason():
    call = classify_trend(_frame("A", V_TURN))
    assert call.reasons == [SMA_CROSSED]
    assert call.metrics["close"] > call.metrics["sma_50"] > call.metrics["sma_200"]
    assert call.metrics["adx_14"] >= 25 and call.metrics["crossed_recently"] is True


def test_short_history_and_no_bars():
    short = classify_trend(_frame("A", RISING[:199]))
    assert (short.label, short.reasons, short.metrics["bars"]) == (UNCLASSIFIED, [SHORT_HISTORY], 199)
    assert classify_trend(_frame("A", RISING[:200])).label == UP
    assert classify_trend(_frame("A", [])).reasons == [NO_BARS]


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
    "UPP": pd.concat([_frame("UPP", RISING), _after_as_of("UPP", [10.0] * 30)], ignore_index=True),
    "DWN": _frame("DWN", FALLING),
    "CHOP": _frame("CHOP", CHOP),
    "VTRN": _frame("VTRN", V_TURN),
    "NEW": _frame("NEW", RISING[:120]),
    "OLD": _frame("OLD", RISING, end=date(2026, 9, 29)),
    "LOSS": _frame("LOSS", FALLING),
}


def _run(strong=("UPP", "DWN", "CHOP", "VTRN", "NEW", "GONE", "OLD"), weak=("LOSS",)):
    fetcher = FakeBars(FRAMES)
    return run_trend(_financials(strong, weak), "financials.json", bars_fetcher=fetcher, created_at=CREATED), fetcher


def test_run_classifies_strong_survivors_only_and_counts_every_name():
    report, fetcher = _run()
    assert fetcher.requested[0][0] == ["UPP", "DWN", "CHOP", "VTRN", "NEW", "GONE", "OLD"]
    assert fetcher.requested[0][2] == AS_OF and fetcher.requested[0][3] == "sip"
    assert "LOSS" not in report.classifications
    assert report.entered == 7
    assert report.label_counts == {UP: 2, DOWN: 1, UNCLASSIFIED: 4}
    assert sum(report.label_counts.values()) == report.entered
    assert report.up == ["OLD", "UPP"] and report.down == ["DWN"]
    assert report.classifications["GONE"]["reasons"] == [NO_BARS]
    assert report.classifications["NEW"]["reasons"] == [SHORT_HISTORY]
    assert report.reason_counts[SMA_CROSSED] == 1 and report.reason_counts[NO_BARS] == 1
    assert report.tangled_only == 1


def test_bars_after_as_of_are_ignored():
    report, _ = _run()
    upp = report.classifications["UPP"]
    assert upp["label"] == UP and upp["metrics"]["last_bar_date"] == "2026-09-30"
    assert upp["metrics"]["bars"] == 260


def test_warnings_carry_point_in_time_and_flag_a_missing_last_bar():
    report, _ = _run()
    assert any("not point in time" in w for w in report.warnings)
    assert any("no bar on 2026-09-30: OLD" in w for w in report.warnings)


def test_report_round_trips_and_never_overwrites(tmp_path):
    report, _ = _run()
    path = save_report(report, tmp_path)
    assert path.name == "trend_2026-09-30_20261006T120500Z.json"
    assert load_report(path) == report
    assert json.loads(path.read_text())["policy"]["adx_trend_min"] == 25.0
    with pytest.raises(FileExistsError):
        save_report(report, tmp_path)


def test_cli_picks_the_newest_financials_report_and_writes(tmp_path, monkeypatch, capsys):
    save_financials(_financials(["UPP", "DWN"]), tmp_path)
    monkeypatch.setattr(cli, "run_trend",
                        lambda fin, path, cache_dir=None: run_trend(fin, path, bars_fetcher=FakeBars(FRAMES),
                                                                    created_at=CREATED))
    out = tmp_path / "out"
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path), "--out-dir", str(out)]) == cli.EXIT_OK
    text = capsys.readouterr().out
    assert "UP:   UPP" in text and "DOWN: DWN" in text
    assert len(list(out.glob("trend_2026-09-30_*.json"))) == 1


def test_cli_without_a_financials_report_is_bad_input(tmp_path):
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path)]) == cli.EXIT_BAD_INPUT
