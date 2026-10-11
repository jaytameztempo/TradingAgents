"""The daily command runs the existing steps in order, stops unless SIDE, stops on unmapped sectors, and never orders.

Steps are replaced by a fake runner that records them and writes the route and
channel files the gates read, under a tmp home folder. No step process is started.
"""

import ast
import json
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from extensions.router.strategy_router import RouteDecision, save_decision
from extensions.scripts import run_daily

AS_OF = date(2026, 10, 9)
T0 = datetime(2026, 10, 10, 22, 0, tzinfo=UTC)
ORDER = ["regime SPY", "regime QQQ", "router", "universe", "financials", "long channel", "short channel",
         "channel rank", "ranked side plans", "4-hour confirm", "daily status", "earnings blackout", "paper tickets"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert Path.home() == tmp_path
    return tmp_path


@pytest.fixture
def guards(monkeypatch):
    """Record the order-path guards instead of installing an import hook in the test process."""
    calls = []
    monkeypatch.setattr(run_daily, "refuse_trading_client", lambda: calls.append("refuse_trading_client"))
    monkeypatch.setattr(run_daily, "_assert_no_order_path", lambda: calls.append("assert_no_order_path"))
    return calls


def _write_route(home, label):
    route = RouteDecision(
        as_of=AS_OF, created_at=T0, series="ALL", regime_symbol="SPY+QQQ", regime_label=label,
        up_allowed=label == "UP", down_allowed=label == "DOWN", tradeable=[], blocked=[],
        reason=f"market is {label}", basket_file=None, regime_file="r.json", regime_labels={"SPY": label, "QQQ": label},
    )
    save_decision(route, home / ".tradingagents" / "routes")


def _write_channel(home, prefix, members, created="20261011T004405Z"):
    folder = home / ".tradingagents" / "scanbot"
    folder.mkdir(parents=True, exist_ok=True)
    data = {"as_of": AS_OF.isoformat(), "created_at": datetime.strptime(created, "%Y%m%dT%H%M%SZ")
            .replace(tzinfo=UTC).isoformat(), "members": members}
    (folder / f"{prefix}_{AS_OF}_{created}.json").write_text(json.dumps(data), encoding="utf-8")


def _fake_runner(home, monkeypatch, label="SIDE", longs=("PGY", "ALGM"), shorts=("ADPT",), fail_at=None):
    ran = []

    def run(step, argv):
        ran.append((step.name, step.module, argv))
        if step.name == fail_at:
            return 1
        if step.name == "router":
            _write_route(home, label)
        if step.name == "long channel":
            _write_channel(home, "long_channel", list(longs))
        if step.name == "short channel":
            _write_channel(home, "short_channel", list(shorts))
        return 0

    monkeypatch.setattr(run_daily, "_run_step", run)
    return ran


@pytest.mark.unit
def test_a_side_day_runs_every_step_in_order(home, guards, monkeypatch, capsys):
    ran = _fake_runner(home, monkeypatch)
    assert run_daily.main(["--as-of", "2026-10-09"]) == run_daily.EXIT_OK
    assert [name for name, _, _ in ran] == ORDER
    assert [m for _, m, _ in ran] == [
        "extensions.scripts.run_regime_bot", "extensions.scripts.run_regime_bot", "extensions.scripts.run_router",
        "extensions.scripts.run_scanbot_universe", "extensions.scripts.run_scanbot_financials",
        "extensions.scripts.run_scanbot_long_channel", "extensions.scripts.run_scanbot_short_channel",
        "extensions.scripts.run_scanbot_channel_rank", "extensions.scripts.run_ranked_side_handoff",
        "extensions.scripts.run_side_confirm_4h", "extensions.scripts.run_daily_status",
        "extensions.scripts.run_earnings_blackout", "extensions.scripts.run_paper_tickets",
    ]
    assert all(argv[:2] == ["--as-of", "2026-10-09"] for _, _, argv in ran)
    assert ran[0][2][2:] == ["--symbol", "SPY"] and ran[1][2][2:] == ["--symbol", "QQQ"]
    assert guards == ["assert_no_order_path", "refuse_trading_client", "assert_no_order_path"]
    out = capsys.readouterr().out
    assert "daily run 2026-10-09: complete" in out and "no order sent" in out


@pytest.mark.unit
@pytest.mark.parametrize("label", ["UP", "DOWN"])
def test_a_label_other_than_side_stops_after_the_router(home, guards, monkeypatch, capsys, label):
    ran = _fake_runner(home, monkeypatch, label=label)
    assert run_daily.main(["--as-of", "2026-10-09"]) == run_daily.EXIT_OK
    assert [name for name, _, _ in ran] == ["regime SPY", "regime QQQ", "router"]
    out = capsys.readouterr().out
    assert f"stopped: SPY+QQQ market label is {label}, not SIDE" in out
    assert "paper tickets" not in out


@pytest.mark.unit
def test_no_route_stops_after_the_router(home, guards, monkeypatch, capsys):
    ran = _fake_runner(home, monkeypatch)
    monkeypatch.setattr(run_daily, "_run_step", lambda step, argv: ran.append((step.name, None, argv)) or 0)
    assert run_daily.main(["--as-of", "2026-10-09"]) == run_daily.EXIT_FAILED
    assert [name for name, _, _ in ran] == ["regime SPY", "regime QQQ", "router"]
    assert "no SPY+QQQ route decision" in capsys.readouterr().err


@pytest.mark.unit
def test_names_missing_from_the_sector_map_stop_before_ranking(home, guards, monkeypatch, capsys):
    ran = _fake_runner(home, monkeypatch, longs=("PGY", "NEWCO1"), shorts=("ADPT", "NEWCO2", "NEWCO1"))
    assert run_daily.main(["--as-of", "2026-10-09"]) == run_daily.EXIT_FAILED
    assert [name for name, _, _ in ran] == ORDER[:ORDER.index("channel rank")]
    out = capsys.readouterr().out
    assert "names missing from the sector map; nothing ranked" in out
    listed = out.split("then rerun):")[1].split()
    assert listed == ["NEWCO1", "NEWCO2"]
    assert "Unknown" not in out


@pytest.mark.unit
def test_the_sector_check_reads_the_newest_channel_reports(home):
    _write_channel(home, "long_channel", ["NEWCO1"], created="20261011T000000Z")
    _write_channel(home, "long_channel", ["PGY"], created="20261011T010000Z")
    _write_channel(home, "short_channel", ["ADPT"])
    assert run_daily.missing_sectors(AS_OF) == []


@pytest.mark.unit
@pytest.mark.parametrize("fail_at", ["regime QQQ", "financials", "ranked side plans", "earnings blackout"])
def test_a_failed_step_stops_everything_after_it(home, guards, monkeypatch, capsys, fail_at):
    ran = _fake_runner(home, monkeypatch, fail_at=fail_at)
    assert run_daily.main(["--as-of", "2026-10-09"]) == run_daily.EXIT_FAILED
    assert [name for name, _, _ in ran] == ORDER[:ORDER.index(fail_at) + 1]
    assert "FAILED (exit 1)" in capsys.readouterr().out


@pytest.mark.unit
def test_options_reach_the_steps_that_take_them(home, guards, monkeypatch):
    ran = _fake_runner(home, monkeypatch)
    run_daily.main(["--as-of", "2026-10-09", "--cache-dir", "c", "--offline", "--preset", "CONSERVATIVE"])
    argv = {name: a for name, _, a in ran}
    for name in ("regime SPY", "regime QQQ", "universe", "long channel", "short channel", "4-hour confirm"):
        assert argv[name][-2:] == ["--cache-dir", "c"], name
    assert "--offline" in argv["financials"] and argv["universe"][2:4] == ["--preset", "CONSERVATIVE"]
    assert argv["paper tickets"] == ["--as-of", "2026-10-09"]


@pytest.mark.unit
def test_bad_date_runs_nothing(guards, monkeypatch):
    monkeypatch.setattr(run_daily, "_run_step", lambda *a: pytest.fail("ran a step"))
    assert run_daily.main(["--as-of", "10/09/2026"]) == run_daily.EXIT_BAD_INPUT


@pytest.mark.unit
def test_a_loaded_trading_client_runs_nothing(monkeypatch):
    def loaded():
        raise run_daily.TradingClientLoaded("alpaca.trading is loaded")

    monkeypatch.setattr(run_daily, "_assert_no_order_path", loaded)
    monkeypatch.setattr(run_daily, "_run_step", lambda *a: pytest.fail("ran a step"))
    assert run_daily.main(["--as-of", "2026-10-09"]) == run_daily.EXIT_BAD_INPUT


@pytest.mark.unit
def test_each_step_runs_as_its_own_python_module(monkeypatch):
    seen = {}

    def fake_run(cmd, cwd, check):
        seen.update(cmd=cmd, cwd=cwd)
        return subprocess.CompletedProcess(cmd, 3)

    monkeypatch.setattr(run_daily.subprocess, "run", fake_run)
    step = run_daily.STEPS_AFTER_SECTOR_GATE[-1]
    assert run_daily._run_step(step, ["--as-of", "2026-10-09"]) == 3
    assert seen["cmd"] == [sys.executable, "-m", "extensions.scripts.run_paper_tickets", "--as-of", "2026-10-09"]
    assert seen["cwd"] == run_daily.REPO_ROOT


@pytest.mark.unit
def test_importing_the_daily_command_loads_no_order_path():
    code = ("import sys, extensions.scripts.run_daily; "
            "print(sorted(m for m in sys.modules if m.startswith(('alpaca', 'paper_orders'))))")
    out = subprocess.run([sys.executable, "-c", code], cwd=run_daily.REPO_ROOT, capture_output=True, text=True,
                         check=True).stdout
    assert out.strip() == "[]"


@pytest.mark.unit
def test_daily_source_never_names_paper_orders_or_an_order_call():
    source = Path(run_daily.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "alpaca" not in node.module and "paper_orders" not in node.module
        if isinstance(node, ast.Import):
            assert not any("alpaca" in a.name or "paper_orders" in a.name for a in node.names)
    for word in ("TradingClient", "submit_order", "OrderRequest", "close_position", "sender"):
        assert not re.search(rf"\b{word}\b", source), word  # TradingClientLoaded is the guard's own error
    modules = [s.module for s in run_daily.STEPS_BEFORE_SIDE_GATE + run_daily.STEPS_BEFORE_SECTOR_GATE
               + run_daily.STEPS_AFTER_SECTOR_GATE]
    assert all(m.startswith("extensions.scripts.") for m in modules)
