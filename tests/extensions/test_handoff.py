"""The handoff plans only names whose side matches the market regime, blocks the rest, and never orders.

Inputs are saved 4-hour reports and routes in tmp folders; Alpaca is made to fail if touched.
"""

import ast
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alpaca.data.historical import StockHistoricalDataClient

from extensions.research import handoff as handoff_mod
from extensions.research.handoff import (
    BLOCKED,
    PLAN,
    PLAN_ENDING,
    build_handoff,
    handoff_for_date,
    parse_delisted,
    plan_text,
    save_handoff,
)
from extensions.router.strategy_router import MissingInput, RouteDecision, save_decision
from extensions.scanbot import confirm_4h
from extensions.scanbot.confirm_4h import Confirm4hReport, save_report
from extensions.scripts import run_handoff as cli

AS_OF = date(2026, 9, 30)
T0 = datetime(2026, 10, 8, 0, 50, tzinfo=UTC)
CONFIRMED = {"UP": ["DINO", "QRVO"], "DOWN": ["CCL", "WH"]}


@pytest.fixture(autouse=True)
def _no_alpaca(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("touched Alpaca")

    monkeypatch.setattr(StockHistoricalDataClient, "__init__", refuse)
    monkeypatch.setattr(confirm_4h, "fetch_4hour_bars", refuse)


def _call(side, symbol):
    return {"side": side, "confirm_pass": True, "reasons": [], "detail": [f"{symbol} 4-hour {side} detail"],
            "metrics": {}}


def _report(confirmed=CONFIRMED, created_at=T0, as_of=AS_OF):
    return Confirm4hReport(
        as_of=as_of,
        created_at=created_at,
        pivot_report="pivot.json",
        pivot_created_at=T0.isoformat(),
        policy={},
        counts={},
        confirmed={side: list(names) for side, names in confirmed.items()},
        calls={side: {s: _call(side, s) for s in names} for side, names in confirmed.items()},
        daily_fails={"UP": [], "DOWN": []},
        warnings=["not point in time"],
    )


def _route(label="SIDE", created_at=T0, as_of=AS_OF, up_passed=("MSFT", "NVDA")):
    up_passed = list(up_passed)
    return RouteDecision(
        as_of=as_of,
        created_at=created_at,
        series="ALL",
        regime_symbol="SPY+QQQ",
        regime_label=label,
        up_allowed=label == "UP",
        down_allowed=label == "DOWN",
        tradeable=up_passed if label == "UP" else [],
        blocked=up_passed if label == "SIDE" else [],
        reason=f"market is {label}",
        basket_file="b.json",
        regime_file="r.json",
        up_passed=up_passed,
        regime_labels={"SPY": label, "QQQ": label},
    )


def _build(label, delisted=None):
    return build_handoff(_report(), "c.json", _route(label), "r.json", delisted)


@pytest.mark.unit
def test_side_market_blocks_every_name_and_writes_no_plan(tmp_path):
    h = _build("SIDE")
    assert h.planned == []
    assert h.blocked == ["DINO", "QRVO", "CCL", "WH"]
    assert all("does not match" in n["reasons"][0] for n in h.names.values())
    path, plans = save_handoff(h, tmp_path / "h", tmp_path / "p")
    assert plans == [] and not (tmp_path / "p").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["orders_placed"] is False
    assert all(n["plan_file"] is None for n in data["names"].values())


@pytest.mark.unit
@pytest.mark.parametrize("label, planned, blocked", [
    ("UP", ["DINO", "QRVO"], ["CCL", "WH"]),
    ("DOWN", ["CCL", "WH"], ["DINO", "QRVO"]),
])
def test_only_the_matching_side_gets_plans(tmp_path, label, planned, blocked):
    h = _build(label)
    assert (h.planned, h.blocked) == (planned, blocked)
    _, plans = save_handoff(h, tmp_path / "h", tmp_path / "p")
    assert sorted(p.name.split("_")[1] for p in plans) == sorted(planned)
    for plan in plans:
        assert plan.read_text(encoding="utf-8").rstrip().endswith(PLAN_ENDING)


@pytest.mark.unit
@pytest.mark.parametrize("label", ["UP", "DOWN", "SIDE"])
def test_a_delisted_name_never_gets_a_plan(tmp_path, label):
    h = _build(label, parse_delisted(["QRVO:2026-10-05"]))
    qrvo = h.names["QRVO"]
    assert qrvo["status"] == BLOCKED
    assert qrvo["delisted"] == {"after": "2026-10-05", "source": "operator"}
    assert "delisted after 2026-10-05; no plan" in qrvo["reasons"]
    with pytest.raises(ValueError, match="gets no plan"):
        plan_text(h, "QRVO")
    _, plans = save_handoff(h, tmp_path / "h", tmp_path / "p")
    assert not any("QRVO" in p.name for p in plans)


@pytest.mark.unit
def test_dino_still_planned_under_up_when_qrvo_is_delisted():
    h = _build("UP", {"QRVO": date(2026, 10, 5)})
    assert h.names["DINO"]["status"] == PLAN
    assert h.planned == ["DINO"]


@pytest.mark.unit
def test_route_passers_are_recorded_not_routed():
    h = _build("SIDE")
    assert h.route_passers_not_confirmed == ["MSFT", "NVDA"]
    assert "MSFT" not in h.names


@pytest.mark.unit
def test_mismatched_dates_are_refused():
    with pytest.raises(ValueError, match="route is for"):
        build_handoff(_report(), "c.json", _route(as_of=date(2026, 10, 1)), "r.json")


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["QRVO", "QRVO:10/05/2026", ":2026-10-05"])
def test_bad_delisted_input_is_refused(bad):
    with pytest.raises(ValueError):
        parse_delisted([bad])


@pytest.mark.unit
def test_newest_report_and_route_are_used(tmp_path):
    reports, routes = tmp_path / "scanbot", tmp_path / "routes"
    save_report(_report({"UP": ["OLD"], "DOWN": []}, created_at=T0), reports)
    save_report(_report(created_at=T0 + timedelta(hours=1)), reports)
    save_decision(_route("UP", created_at=T0 + timedelta(hours=1)), routes)
    save_decision(_route("SIDE", created_at=T0), routes)
    h = handoff_for_date(AS_OF, None, reports, routes)
    assert h.regime_label == "UP"
    assert list(h.names) == ["DINO", "QRVO", "CCL", "WH"]


@pytest.mark.unit
def test_missing_inputs_stop_the_handoff(tmp_path):
    with pytest.raises(MissingInput, match="no 4-hour report"):
        handoff_for_date(AS_OF, None, tmp_path, tmp_path)


@pytest.mark.unit
def test_cli_writes_handoff_and_no_plan_under_side(tmp_path, capsys):
    save_report(_report(), tmp_path / "scanbot")
    save_decision(_route("SIDE"), tmp_path / "routes")
    code = cli.main(["--as-of", "2026-09-30", "--delisted", "QRVO:2026-10-05",
                     "--report-dir", str(tmp_path / "scanbot"), "--route-dir", str(tmp_path / "routes"),
                     "--out-dir", str(tmp_path / "h"), "--plan-dir", str(tmp_path / "p")])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "QRVO (UP): BLOCKED  [delisted after 2026-10-05]" in out
    assert "plans written: 0; no order placed" in out
    assert len(list((tmp_path / "h").glob("handoff_2026-09-30_*.json"))) == 1
    assert not (tmp_path / "p").exists()


@pytest.mark.unit
def test_cli_rejects_bad_delisted(capsys):
    assert cli.main(["--as-of", "2026-09-30", "--delisted", "QRVO"]) == cli.EXIT_BAD_INPUT


@pytest.mark.unit
def test_handoff_sources_never_name_alpaca_or_an_order_call():
    for path in (Path(handoff_mod.__file__), Path(cli.__file__)):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "alpaca" not in node.module, path
            if isinstance(node, ast.Import):
                assert not any("alpaca" in a.name for a in node.names), path
        for word in ("TradingClient", "submit_order", "OrderRequest", "close_position"):
            assert word not in source, (path, word)
