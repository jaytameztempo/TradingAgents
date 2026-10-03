"""SIDEBot writes a research plan only when the route's regime is SIDE, and never touches Alpaca.

Route files are written here as plain JSON, so SIDEBot's own tests import no Alpaca module;
one test checks the router's real output stays readable.
"""

import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from extensions.playbooks import side_bot
from extensions.playbooks.side_bot import (
    MissingRoute,
    RegimeNotSide,
    SymbolNotInRoute,
    latest_route,
    make_plan,
    render,
    save_plan,
)
from extensions.scripts import run_side_bot as cli

AS_OF = date(2026, 9, 30)
T0 = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _route(label="SIDE", tickers=("NVDA", "MSFT"), as_of=AS_OF, created_at=T0):
    up = label == "UP"
    tickers = list(tickers)
    return {
        "as_of": as_of.isoformat(),
        "created_at": created_at.isoformat(),
        "series": "UP",
        "regime_symbol": "SPY",
        "regime_label": label,
        "up_allowed": up,
        "tradeable": tickers if up else [],
        "blocked": [] if up else tickers,
        "reason": f"SPY regime is {label}",
        "basket_file": "b.json",
        "regime_file": "r.json",
        "schema_version": 1,
    }


def _write_route(folder, data):
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(data["created_at"])
    path = folder / f"route_UP_{data['as_of']}_{stamp:%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _argv(route_dir, out_dir, symbol="NVDA"):
    return ["--as-of", "2026-09-30", "--symbol", symbol, "--route-dir", str(route_dir), "--out-dir", str(out_dir)]


@pytest.mark.unit
def test_side_writes_one_plan_with_the_required_lines(tmp_path, capsys):
    routes, plans = tmp_path / "routes", tmp_path / "plans"
    route_file = _write_route(routes, _route("SIDE"))

    assert cli.main(_argv(routes, plans)) == cli.EXIT_OK

    (plan_file,) = plans.iterdir()
    lines = plan_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "SIDEBot plan (research only)"
    assert "symbol: NVDA" in lines
    assert "date: 2026-09-30" in lines
    assert "regime: SIDE (SPY)" in lines
    assert f"route file: {route_file}" in lines
    assert any(line.startswith("mean-reversion note: ") and "prefer cash" in line for line in lines)
    assert lines[-1] == "no order placed."
    assert f"wrote {plan_file}" in capsys.readouterr().out


@pytest.mark.unit
def test_a_blocked_name_is_plannable_under_side(tmp_path):
    route = side_bot.Route.from_dict(_route("SIDE", tickers=("AAPL",)))
    assert route.blocked == ["AAPL"] and route.tradeable == []

    assert make_plan(route, "r.json", "aapl").symbol == "AAPL"


@pytest.mark.unit
@pytest.mark.parametrize("label", ["UP", "DOWN"])
def test_up_or_down_stops_and_writes_no_plan(label, tmp_path, capsys):
    routes, plans = tmp_path / "routes", tmp_path / "plans"
    _write_route(routes, _route(label))

    assert cli.main(_argv(routes, plans)) == cli.EXIT_WRONG_REGIME

    assert f"SIDEBot blocked: regime is {label}; no plan written" in capsys.readouterr().out
    assert not plans.exists()


@pytest.mark.unit
@pytest.mark.parametrize("label", ["UP", "DOWN", "side", "UNKNOWN"])
def test_make_plan_refuses_every_label_but_side(label):
    route = side_bot.Route.from_dict(_route(label))
    with pytest.raises(RegimeNotSide, match=f"regime is {label}"):
        make_plan(route, "r.json", "NVDA")


@pytest.mark.unit
def test_the_regime_is_checked_before_the_symbol():
    """Under UP an unrouted symbol still reports the regime, so the reason recorded is the hard reject."""
    with pytest.raises(RegimeNotSide):
        make_plan(side_bot.Route.from_dict(_route("UP")), "r.json", "AAPL")


@pytest.mark.unit
def test_a_symbol_outside_the_route_is_refused(tmp_path, capsys):
    routes, plans = tmp_path / "routes", tmp_path / "plans"
    _write_route(routes, _route("SIDE"))

    assert cli.main(_argv(routes, plans, "AAPL")) == cli.EXIT_FAILED

    assert "AAPL is not in the route for 2026-09-30" in capsys.readouterr().err
    assert not plans.exists()
    with pytest.raises(SymbolNotInRoute):
        make_plan(side_bot.Route.from_dict(_route("SIDE")), "r.json", "AAPL")


@pytest.mark.unit
def test_the_newest_route_for_the_date_wins(tmp_path):
    _write_route(tmp_path, _route("SIDE", created_at=T0))
    _write_route(tmp_path, _route("UP", created_at=T0 + timedelta(hours=1)))
    _write_route(tmp_path, _route("SIDE", created_at=T0 + timedelta(hours=2), as_of=AS_OF - timedelta(days=1)))

    _, route = latest_route(AS_OF, tmp_path)

    assert route.regime_label == "UP"


@pytest.mark.unit
def test_a_missing_route_stops(tmp_path, capsys):
    with pytest.raises(MissingRoute, match="no route decision for 2026-09-30"):
        latest_route(AS_OF, tmp_path)
    assert cli.main(_argv(tmp_path / "routes", tmp_path / "plans")) == cli.EXIT_FAILED
    assert "no plan written" in capsys.readouterr().err
    assert not (tmp_path / "plans").exists()


@pytest.mark.unit
def test_an_unreadable_route_file_stops(tmp_path):
    _write_route(tmp_path, _route("SIDE"))
    (tmp_path / "route_UP_2026-09-30_20260930T230000Z.json").write_text("{", encoding="utf-8")
    with pytest.raises(MissingRoute, match="cannot read route file"):
        latest_route(AS_OF, tmp_path)


@pytest.mark.unit
def test_an_unknown_route_schema_stops(tmp_path):
    _write_route(tmp_path, {**_route("SIDE"), "schema_version": 2})
    with pytest.raises(MissingRoute, match="schema_version"):
        latest_route(AS_OF, tmp_path)


@pytest.mark.unit
def test_an_existing_plan_is_never_overwritten(tmp_path):
    plan = make_plan(side_bot.Route.from_dict(_route("SIDE")), "r.json", "NVDA", created_at=T0)
    path = save_plan(plan, tmp_path)
    with pytest.raises(FileExistsError):
        save_plan(plan, tmp_path)
    assert path.read_text(encoding="utf-8") == render(plan)


@pytest.mark.unit
@pytest.mark.parametrize("argv", [
    ["--as-of", "30/09/2026", "--symbol", "NVDA"],
    ["--as-of", "2026-09-30", "--symbol", "NVDA;rm"],
])
def test_cli_refuses_bad_input(argv, tmp_path):
    assert cli.main([*argv, "--out-dir", str(tmp_path / "plans")]) == cli.EXIT_BAD_INPUT
    assert not (tmp_path / "plans").exists()


@pytest.mark.unit
def test_sidebot_imports_no_alpaca_module():
    """In a fresh interpreter, loading SIDEBot and its script leaves no alpaca module loaded at all."""
    code = (
        "import sys, extensions.playbooks.side_bot, extensions.scripts.run_side_bot; "
        "print(sorted(m for m in sys.modules if m == 'alpaca' or m.startswith('alpaca.')))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


@pytest.mark.unit
def test_the_router_output_is_readable(tmp_path):
    """The route SIDEBot reads is the one strategy_router writes; this keeps the two from drifting."""
    from extensions.router.strategy_router import RouteDecision, save_decision

    decision = RouteDecision(
        as_of=AS_OF, created_at=T0, series="UP", regime_symbol="SPY", regime_label="SIDE",
        up_allowed=False, tradeable=[], blocked=["NVDA"], reason="SPY regime is SIDE",
        basket_file="b.json", regime_file="r.json",
    )
    path = save_decision(decision, tmp_path)

    found_path, route = latest_route(AS_OF, tmp_path)

    assert found_path == path
    assert (route.regime_label, route.blocked, route.created_at) == ("SIDE", ["NVDA"], T0)
