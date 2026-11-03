"""The setup check: each line passes, warns or fails as it should. A fake reader stands in for the paper account."""

import json

import pytest

from extensions.scripts import check_setup
from paper_orders.snapshot import ReadFailed

PAPER = {"ALPACA_API_KEY": "PKTEST000", "ALPACA_SECRET_KEY": "s"}


class Reader:
    def __init__(self, fail=False):
        self.fail = fail

    def account(self):
        if self.fail:
            raise ReadFailed("GET /v2/account: HTTP 401: unauthorized")
        return {"equity": "100000", "status": "ACTIVE"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_UI_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("TRADINGAGENTS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _lines(home, env=PAPER, reader=None, repo=None):
    return {line.name: line for line in check_setup.check(env, lambda e: reader or Reader(), repo or home / "repo")}


def _settings(home, data):
    (home / "settings.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.unit
def test_a_ready_machine_passes_every_line(home):
    _settings(home, {"trading": {"days": ["mon"]}, "tradebot": {"mode": "semi"}})
    lines = _lines(home)
    assert all(line.result == "PASS" for line in lines.values()), lines
    assert "mode semi" in lines["tradebot settings"].detail and "kill switch off" in lines["risk settings"].detail


@pytest.mark.unit
def test_a_live_or_missing_key_fails_and_skips_the_account(home):
    lines = _lines(home, env={"ALPACA_API_KEY": "AKLIVE", "ALPACA_SECRET_KEY": "s"})
    assert lines["paper keys"].result == "FAIL" and lines["paper account"].result == "FAIL"
    assert "not checked" in lines["paper account"].detail


@pytest.mark.unit
def test_the_key_id_is_never_printed(home, capsys):
    check_setup.main(env=PAPER, make_reader=lambda e: Reader())
    assert "PKTEST000" not in capsys.readouterr().out


@pytest.mark.unit
def test_an_account_that_does_not_answer_fails(home):
    assert _lines(home, reader=Reader(fail=True))["paper account"].result == "FAIL"


@pytest.mark.unit
def test_no_trading_window_warns(home):
    line = _lines(home)["trading window"]
    assert line.result == "WARN" and "auto mode is locked" in line.detail


@pytest.mark.unit
def test_bad_risk_or_tradebot_settings_fail(home):
    _settings(home, {"risk": {"drawdown_pct": "ten"}, "tradebot": {"mode": "yolo"}})
    lines = _lines(home)
    assert lines["risk settings"].result == "FAIL" and lines["tradebot settings"].result == "FAIL"


@pytest.mark.unit
def test_a_data_folder_in_onedrive_fails_and_a_repo_in_onedrive_warns(home, monkeypatch):
    monkeypatch.setenv("OneDrive", str(home))
    lines = _lines(home)
    assert lines["data folder"].result == "FAIL" and "refuse to run" in lines["data folder"].detail
    assert lines["repo"].result == "WARN" and lines[".venv"].result == "WARN"


@pytest.mark.unit
def test_an_unreadable_scheduler_state_warns(home):
    (home / "scheduler_state.json").write_text("{broken", encoding="utf-8")
    assert _lines(home)["scheduler state"].result == "WARN"


@pytest.mark.unit
def test_the_exit_code_is_one_on_any_fail(home):
    assert check_setup.main(env=PAPER, make_reader=lambda e: Reader()) == 0  # warnings only
    assert check_setup.main(env={}, make_reader=lambda e: Reader()) == 1
