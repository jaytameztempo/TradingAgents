"""run_tradebot: the stages end to end on saved files, with the bar and account readers faked."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from extensions.scripts import run_tradebot
from extensions.status import confirmation
from extensions.status.confirmation import Confirmation, MarketData
from extensions.status.earnings_blackout import CLEAR, NameCheck
from extensions.status.paper_tickets import Account
from extensions.tradebot import store, triggers

AS_OF = date(2026, 9, 30)
BASKET = f"scan-up-category-1_{AS_OF}_20261001T000000Z.json"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(store.HOME_ENV, str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    # other tests in the run may have loaded alpaca.trading; the real guard is checked in its own process below
    monkeypatch.setattr(run_tradebot, "refuse_trading_client", lambda: None)
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)
    _write(tmp_path / "routes" / f"route_ALL_{AS_OF}_20261001T000000Z.json",
           {"as_of": AS_OF.isoformat(), "created_at": "2026-09-30T22:00:00+00:00", "regime_symbol": "SPY+QQQ",
            "regime_label": "UP", "schema_version": 1})
    _write(tmp_path / "baskets" / BASKET, {"scan_name": "SCAN-UP Category 1", "as_of": AS_OF.isoformat(),
                                           "created_at": "2026-10-01T00:00:00+00:00",
                                           "members": [{"ticker": "X", "score": 0.1, "metrics": {}}]})
    item = store.new_item(ticker="X", as_of=AS_OF.isoformat(), kind="up_cat", scan="s", scan_file=BASKET,
                          side="long", needs_regime="UP", metrics={}, source="auto")
    store.enqueue("trade", [item])
    monkeypatch.setattr(triggers, "check_name", lambda s, side, d, _: (
        NameCheck(s, side, "2026-11-20", "estimated", "e", 36, CLEAR, None, "2026-09-29T00:00:00+00:00"), []))
    monkeypatch.setattr(confirmation, "confirm", lambda s, side, m, d: Confirmation(
        s, side, True, [], ["rsi"], {}, 40.0, 900.0, 1000.0, d.isoformat()))
    return tmp_path


def _market(symbols, as_of, levels_as_of):
    return MarketData(pd.DataFrame(), pd.DataFrame(), "test"), pd.DataFrame()


def _no_readers():
    raise AssertionError("the account must not be read")


def _main(argv, now=datetime(2026, 9, 30, 21, 0, tzinfo=UTC)):
    return run_tradebot.main(argv, now=now, market_reader=_market, readers=_no_readers)


@pytest.mark.unit
def test_eod_arms_and_saves_the_check(home, capsys):
    assert _main(["--stage", "eod"]) == 0  # 17:00 New York on a trading day: as_of is today
    item = store.read()["trade"][0]
    assert item["status"] == "armed" and item["checks"][-1]["stage"] == "eod"
    assert "waiting -> armed" in capsys.readouterr().out


@pytest.mark.unit
def test_a_dry_run_saves_nothing(home):
    assert _main(["--stage", "eod", "--dry-run", "--json"]) == 0
    item = store.read()["trade"][0]
    assert item["status"] == "waiting" and item["checks"] == []


@pytest.mark.unit
def test_before_the_close_eod_checks_the_last_session(home, capsys):
    assert _main(["--stage", "eod", "--json"], now=datetime(2026, 10, 1, 14, 0, tzinfo=UTC)) == 0
    assert json.loads(capsys.readouterr().out)["as_of"] == "2026-09-30"


@pytest.mark.unit
def test_intraday_does_not_run_on_an_early_close(home, capsys):
    assert _main(["--stage", "intraday", "--as-of", "2026-11-27", "--json"]) == 0  # the day after Thanksgiving
    out = json.loads(capsys.readouterr().out)
    assert out["checks"] == [] and "closes early" in out["notes"][0]


@pytest.mark.unit
def test_a_status_changed_during_the_check_is_left_alone(home, monkeypatch):
    real = run_tradebot._apply

    def racing(checks, seen):
        with store.transaction() as q:
            store.set_status(q["trade"][0], "removed", "the operator removed it meanwhile")
        return real(checks, seen)

    monkeypatch.setattr(run_tradebot, "_apply", racing)
    assert _main(["--stage", "eod"]) == 0
    item = store.read()["trade"][0]
    assert item["status"] == "removed" and item["checks"] == []


@pytest.mark.unit
def test_a_data_home_inside_onedrive_is_refused(home, monkeypatch, capsys):
    monkeypatch.setenv("OneDrive", str(home.parent))
    assert _main(["--stage", "eod"]) == 2
    assert "inside %OneDrive%" in capsys.readouterr().err
    assert store.read()["trade"][0]["status"] == "waiting"


@pytest.mark.unit
def test_a_ticket_failure_is_reported_and_writes_nothing(home, capsys):
    def readers():
        def account():
            return Account(100_000.0, 100_000.0, datetime.now(UTC), "test")
        return account, lambda: [{"symbol": "HELD", "qty": "10", "current_price": "5"}], list, None

    with store.transaction() as q:
        store.set_status(q["trade"][0], "triggered", "test")
    code = run_tradebot.main(["--ticket-only"], now=datetime(2026, 9, 30, 21, tzinfo=UTC), market_reader=_market,
                             readers=readers)
    assert code == 1 and "unprotected position HELD" in capsys.readouterr().err
    assert store.read()["trade"][0]["status"] == "triggered"


@pytest.mark.unit
def test_the_command_refuses_to_run_with_the_trading_client_loaded(tmp_path):
    import subprocess
    import sys

    code = ("import alpaca.trading.client, sys; from extensions.scripts import run_tradebot; "
            "sys.exit(run_tradebot.main(['--stage', 'eod']))")
    env = {**__import__("os").environ, store.HOME_ENV: str(tmp_path)}
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(run_tradebot.__file__).resolve().parents[2], env=env,
                         capture_output=True, text=True)
    assert out.returncode == 2 and "trading client is already loaded" in out.stderr
