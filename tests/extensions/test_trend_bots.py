"""UPBot and DOWNBot write a research plan only on their own regime, and never touch Alpaca.

Route files are written here as plain JSON, so these tests import no Alpaca module.
"""

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from extensions.playbooks.side_bot import Route
from extensions.playbooks.trend_bots import (
    DOWNBOT,
    UPBOT,
    RegimeMismatch,
    SymbolNotInRoute,
    make_plan,
    render,
    save_plan,
)
from extensions.scripts import run_down_bot, run_side_bot, run_up_bot, trend_bot_cli

AS_OF = date(2026, 9, 30)
T0 = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
LABELS = ("UP", "DOWN", "SIDE")

BOTS = [
    pytest.param(UPBOT, run_up_bot, id="UPBot"),
    pytest.param(DOWNBOT, run_down_bot, id="DOWNBot"),
]


def _route(label, tickers=("NVDA", "MSFT"), as_of=AS_OF, created_at=T0):
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
@pytest.mark.parametrize("bot, cli", BOTS)
def test_the_matching_regime_writes_one_plan(bot, cli, tmp_path, capsys):
    routes, plans = tmp_path / "routes", tmp_path / "plans"
    route_file = _write_route(routes, _route(bot.regime))

    assert cli.main(_argv(routes, plans)) == trend_bot_cli.EXIT_OK

    (plan_file,) = plans.iterdir()
    assert plan_file.name.startswith(f"{bot.name.lower()}_NVDA_2026-09-30_")
    lines = plan_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"{bot.name} plan (research only)"
    assert "symbol: NVDA" in lines
    assert "date: 2026-09-30" in lines
    assert f"regime: {bot.regime} (SPY)" in lines
    assert f"route file: {route_file}" in lines
    assert f"trend-following note: {bot.note}" in lines
    assert lines[-1] == "no order placed."
    assert f"wrote {plan_file}" in capsys.readouterr().out


@pytest.mark.unit
@pytest.mark.parametrize("bot, cli", BOTS)
def test_every_other_label_blocks_and_writes_no_plan(bot, cli, tmp_path, capsys):
    for label in [*(lbl for lbl in LABELS if lbl != bot.regime), "UNKNOWN", bot.regime.lower()]:
        routes, plans = tmp_path / label / "routes", tmp_path / label / "plans"
        _write_route(routes, _route(label))

        assert cli.main(_argv(routes, plans)) == trend_bot_cli.EXIT_WRONG_REGIME, label

        assert f"{bot.name} blocked: regime is {label}; no plan written" in capsys.readouterr().out
        assert not plans.exists(), label


@pytest.mark.unit
@pytest.mark.parametrize("bot, cli", BOTS)
def test_the_regime_is_checked_before_the_symbol(bot, cli):
    other = "SIDE"
    with pytest.raises(RegimeMismatch, match=f"regime is {other}"):
        make_plan(bot, Route.from_dict(_route(other)), "r.json", "AAPL")


@pytest.mark.unit
@pytest.mark.parametrize("bot, cli", BOTS)
def test_a_symbol_outside_the_route_is_refused(bot, cli, tmp_path, capsys):
    routes, plans = tmp_path / "routes", tmp_path / "plans"
    _write_route(routes, _route(bot.regime))

    assert cli.main(_argv(routes, plans, "AAPL")) == trend_bot_cli.EXIT_FAILED

    assert "AAPL is not in the route for 2026-09-30" in capsys.readouterr().err
    assert not plans.exists()
    with pytest.raises(SymbolNotInRoute):
        make_plan(bot, Route.from_dict(_route(bot.regime)), "r.json", "AAPL")


@pytest.mark.unit
def test_routed_names_count_whether_tradeable_or_blocked():
    """Under UP the router lists names as tradeable; under DOWN as blocked. Both are routed."""
    up_route = Route.from_dict(_route("UP", tickers=("NVDA",)))
    down_route = Route.from_dict(_route("DOWN", tickers=("NVDA",)))
    assert up_route.tradeable == ["NVDA"] and down_route.blocked == ["NVDA"]

    assert make_plan(UPBOT, up_route, "r.json", "nvda").symbol == "NVDA"
    assert make_plan(DOWNBOT, down_route, "r.json", "nvda").symbol == "NVDA"


@pytest.mark.unit
@pytest.mark.parametrize("bot, cli", BOTS)
def test_a_missing_route_stops(bot, cli, tmp_path, capsys):
    assert cli.main(_argv(tmp_path / "routes", tmp_path / "plans")) == trend_bot_cli.EXIT_FAILED
    assert "no route decision for 2026-09-30" in capsys.readouterr().err
    assert not (tmp_path / "plans").exists()


@pytest.mark.unit
@pytest.mark.parametrize("bot, cli", BOTS)
@pytest.mark.parametrize("argv", [
    ["--as-of", "30/09/2026", "--symbol", "NVDA"],
    ["--as-of", "2026-09-30", "--symbol", "NVDA;rm"],
])
def test_cli_refuses_bad_input(bot, cli, argv, tmp_path):
    assert cli.main([*argv, "--out-dir", str(tmp_path / "plans")]) == trend_bot_cli.EXIT_BAD_INPUT
    assert not (tmp_path / "plans").exists()


@pytest.mark.unit
@pytest.mark.parametrize("bot, cli", BOTS)
def test_an_existing_plan_is_never_overwritten(bot, cli, tmp_path):
    plan = make_plan(bot, Route.from_dict(_route(bot.regime)), "r.json", "NVDA", created_at=T0)
    path = save_plan(plan, tmp_path)
    with pytest.raises(FileExistsError):
        save_plan(plan, tmp_path)
    assert path.read_text(encoding="utf-8") == render(plan)


@pytest.mark.unit
@pytest.mark.parametrize("label, planner", [("UP", "UPBot"), ("DOWN", "DOWNBot"), ("SIDE", "SIDEBot")])
def test_exactly_one_bot_plans_for_each_label(label, planner, tmp_path):
    """A mismatch is a hard reject: on any route, only the matching playbook writes a plan."""
    routes = tmp_path / "routes"
    _write_route(routes, _route(label))
    clis = {"UPBot": run_up_bot, "DOWNBot": run_down_bot, "SIDEBot": run_side_bot}

    wrote = []
    for name, cli in clis.items():
        plans = tmp_path / name
        if cli.main(_argv(routes, plans)) == 0:
            wrote.append(name)
        assert plans.exists() == (name in wrote), name

    assert wrote == [planner]


@pytest.mark.unit
def test_trend_bots_import_no_alpaca_module():
    """In a fresh interpreter, loading UPBot and DOWNBot and their scripts leaves no alpaca module loaded."""
    code = (
        "import sys, extensions.playbooks.trend_bots, extensions.scripts.trend_bot_cli, "
        "extensions.scripts.run_up_bot, extensions.scripts.run_down_bot; "
        "print(sorted(m for m in sys.modules if m == 'alpaca' or m.startswith('alpaca.')))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"
