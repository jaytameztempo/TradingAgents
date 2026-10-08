"""The SIDE handoff plans channel fades only under a SIDE market, blocks every name otherwise, and never orders.

Inputs are saved channel reports and routes in tmp folders; Alpaca is made to fail if touched.
"""

import ast
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alpaca.data.historical import StockHistoricalDataClient

from extensions.research import side_handoff as side_mod
from extensions.research.side_handoff import (
    BLOCKED,
    LONG_FADE,
    PLAN,
    PLAN_ENDING,
    SHORT_FADE,
    build_side_handoff,
    plan_text,
    save_side_handoff,
    side_handoff_for_date,
)
from extensions.router.strategy_router import MissingInput, RouteDecision, save_decision
from extensions.scanbot import long_channel, short_channel
from extensions.scanbot.long_channel import LongChannelReport
from extensions.scanbot.short_channel import ShortChannelReport
from extensions.scripts import run_side_handoff as cli

AS_OF = date(2026, 9, 30)
T0 = datetime(2026, 10, 8, 2, 0, tzinfo=UTC)
LONGS, SHORTS = ["CI", "MO"], ["IONQ", "VIR"]


@pytest.fixture(autouse=True)
def _no_alpaca(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("touched Alpaca")

    monkeypatch.setattr(StockHistoricalDataClient, "__init__", refuse)
    monkeypatch.setattr(long_channel, "fetch_daily_bars", refuse)
    monkeypatch.setattr(short_channel, "fetch_daily_bars", refuse)


def _call(symbol, short=False):
    metrics = {"close": 100.0, "support": 90.0, "resistance": 101.0, "height_pct": 11.0, "adx_14": 12.0,
               "atr_14": 2.0, "atr_pct": 2.0, "support_touches": [{}, {}], "resistance_touches": [{}, {}, {}],
               "rsi_14": 50.0, "last_bar_date": "2026-09-30"}
    metrics["distance_to_resistance_atr" if short else "distance_to_support_atr"] = -0.5 if short else 5.0
    return {"passed": True, "reasons": [], "detail": [f"{symbol} channel detail"],
            "quality": {"q1": True, "q2": None}, "quality_passed": 1, "metrics": metrics}


def _common(members, created_at, as_of, short):
    return dict(
        basket_id="b", as_of=as_of, created_at=created_at, financials_report="f.json",
        financials_created_at=T0.isoformat(), policy={}, stage_counts={}, reason_counts={},
        quality_counts={}, members=list(members), calls={s: _call(s, short) for s in members},
        warnings=["not point in time"],
    )


def _long(members=LONGS, created_at=T0, as_of=AS_OF):
    return LongChannelReport(scan_name=long_channel.SCAN_NAME, mode=long_channel.MODE, pivot_side="LONG",
                             **_common(members, created_at, as_of, False))


def _short(members=SHORTS, created_at=T0, as_of=AS_OF):
    return ShortChannelReport(scan_name=short_channel.SCAN_NAME, mode=short_channel.MODE, pivot_side="SHORT",
                              financial_gate="WEAK", assets_source="a.json", assets_fetched_at=T0.isoformat(),
                              **_common(members, created_at, as_of, True))


def _route(label="SIDE", created_at=T0, as_of=AS_OF):
    return RouteDecision(
        as_of=as_of, created_at=created_at, series="ALL", regime_symbol="SPY+QQQ", regime_label=label,
        up_allowed=label == "UP", down_allowed=label == "DOWN", tradeable=[], blocked=[],
        reason=f"market is {label}", basket_file="b.json", regime_file="r.json",
        regime_labels={"SPY": label, "QQQ": label},
    )


def _build(label, longs=LONGS, shorts=SHORTS):
    return build_side_handoff(_long(longs), "l.json", _short(shorts), "s.json", _route(label), "r.json")


@pytest.mark.unit
def test_side_market_plans_long_fades_and_short_fades(tmp_path):
    h = _build("SIDE")
    assert h.planned == LONGS + SHORTS and h.blocked == []
    assert {s: h.names[s]["setup"] for s in h.planned} == {
        "CI": LONG_FADE, "MO": LONG_FADE, "IONQ": SHORT_FADE, "VIR": SHORT_FADE}
    path, plans = save_side_handoff(h, tmp_path / "h", tmp_path / "p")
    assert sorted(p.name.split("_")[2] for p in plans) == sorted(LONGS + SHORTS)
    for plan in plans:
        assert plan.read_text(encoding="utf-8").rstrip().endswith(PLAN_ENDING)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["orders_placed"] is False
    assert all(n["plan_file"] for n in data["names"].values())


@pytest.mark.unit
def test_plans_name_the_fade_and_the_line():
    h = _build("SIDE")
    ci, vir = plan_text(h, "CI"), plan_text(h, "VIR")
    assert "long fade off support" in ci and "0.25 ATR through support" in ci
    assert "a close past 89.5 " in ci  # support 90 - 0.25 * ATR 2
    assert "short fade off resistance" in vir and "0.25 ATR through resistance" in vir
    assert "a close past 101.5 " in vir  # resistance 101 + 0.25 * ATR 2
    assert "borrow" in vir


@pytest.mark.unit
@pytest.mark.parametrize("label", ["UP", "DOWN"])
def test_trending_market_blocks_every_name_and_writes_no_plan(tmp_path, label):
    h = _build(label)
    assert h.planned == [] and h.blocked == LONGS + SHORTS
    assert all(f"market regime {label} is not SIDE" in n["reasons"][0] for n in h.names.values())
    with pytest.raises(ValueError, match="gets no plan"):
        plan_text(h, "CI")
    path, plans = save_side_handoff(h, tmp_path / "h", tmp_path / "p")
    assert plans == [] and not (tmp_path / "p").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert all(n["plan_file"] is None for n in data["names"].values())


@pytest.mark.unit
def test_empty_baskets_write_a_handoff_with_no_plans(tmp_path):
    h = _build("SIDE", [], [])
    path, plans = save_side_handoff(h, tmp_path / "h", tmp_path / "p")
    assert plans == [] and path.exists()


@pytest.mark.unit
def test_a_name_in_both_baskets_is_refused():
    with pytest.raises(ValueError, match="both"):
        _build("SIDE", ["CI"], ["CI"])


@pytest.mark.unit
def test_mismatched_dates_are_refused():
    with pytest.raises(ValueError, match="route is for"):
        build_side_handoff(_long(), "l.json", _short(as_of=date(2026, 10, 1)), "s.json", _route(), "r.json")


@pytest.mark.unit
def test_newest_reports_and_route_are_used(tmp_path):
    reports, routes = tmp_path / "scanbot", tmp_path / "routes"
    long_channel.save_report(_long(["OLD"], created_at=T0), reports)
    long_channel.save_report(_long(created_at=T0 + timedelta(hours=1)), reports)
    short_channel.save_report(_short(created_at=T0), reports)
    save_decision(_route("UP", created_at=T0), routes)
    save_decision(_route("SIDE", created_at=T0 + timedelta(hours=1)), routes)
    h = side_handoff_for_date(AS_OF, reports, routes)
    assert h.regime_label == "SIDE"
    assert list(h.names) == LONGS + SHORTS


@pytest.mark.unit
def test_missing_inputs_stop_the_handoff(tmp_path):
    with pytest.raises(MissingInput, match="no long_channel report"):
        side_handoff_for_date(AS_OF, tmp_path, tmp_path)
    long_channel.save_report(_long(), tmp_path)
    with pytest.raises(MissingInput, match="no short_channel report"):
        side_handoff_for_date(AS_OF, tmp_path, tmp_path)


@pytest.mark.unit
def test_cli_writes_handoff_and_plans_under_side(tmp_path, capsys):
    long_channel.save_report(_long(), tmp_path / "scanbot")
    short_channel.save_report(_short(), tmp_path / "scanbot")
    save_decision(_route("SIDE"), tmp_path / "routes")
    code = cli.main(["--as-of", "2026-09-30",
                     "--report-dir", str(tmp_path / "scanbot"), "--route-dir", str(tmp_path / "routes"),
                     "--out-dir", str(tmp_path / "h"), "--plan-dir", str(tmp_path / "p")])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "VIR (SHORT_FADE): PLAN" in out
    assert "plans written: 4; no order placed" in out
    assert len(list((tmp_path / "h").glob("side_handoff_2026-09-30_*.json"))) == 1
    assert len(list((tmp_path / "p").glob("side_research_*.md"))) == 4


@pytest.mark.unit
def test_cli_rejects_bad_date():
    assert cli.main(["--as-of", "09/30/2026"]) == cli.EXIT_BAD_INPUT


@pytest.mark.unit
def test_side_handoff_sources_never_name_alpaca_or_an_order_call():
    for path in (Path(side_mod.__file__), Path(cli.__file__)):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "alpaca" not in node.module, path
            if isinstance(node, ast.Import):
                assert not any("alpaca" in a.name for a in node.names), path
        for word in ("TradingClient", "submit_order", "OrderRequest", "close_position"):
            assert word not in source, (path, word)
