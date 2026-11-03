"""paper_orders.flatten: a dry run changes nothing; --send turns the kill switch on before it cancels and closes."""

import json

import pytest

from paper_orders import flatten, guard, sender
from tests.test_paper_order_sender import PAPER_ENV


class FlattenClient:
    def __init__(self, fail_cancel=False):
        self.calls = []
        self.fail_cancel = fail_cancel

    def get_account(self):
        return {"equity": "1"}

    def get_all_positions(self):
        return [{"symbol": "VST", "side": "long", "qty": "36", "market_value": "4984"}]

    def get_orders(self, filter=None):  # noqa: A002
        return [{"symbol": "VST", "side": "sell", "type": "limit", "order_class": "oco", "qty": "36", "status": "new",
                 "client_order_id": "pt-2026-09-30-VST-exit"}]

    def cancel_orders(self):
        self.calls.append(("cancel_orders", guard.load_risk()["kill_switch"]))
        if self.fail_cancel:
            raise RuntimeError("network down")
        return [{"id": "o1", "status": 200}]

    def close_all_positions(self, cancel_orders=False):
        self.calls.append(("close_all_positions", cancel_orders))
        return [{"symbol": "VST", "status": 200}]


@pytest.fixture(autouse=True)
def settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"trading": {"days": ["mon"]}}), encoding="utf-8")
    monkeypatch.setenv("TRADINGAGENTS_SETTINGS_PATH", str(path))
    return path


@pytest.mark.unit
def test_a_dry_run_lists_the_book_and_changes_nothing(capsys, settings):
    client = FlattenClient()
    assert flatten.main([], env=PAPER_ENV, make_client=lambda env: client) == flatten.EXIT_OK
    assert client.calls == [] and guard.load_risk()["kill_switch"] is False
    out = capsys.readouterr().out
    assert "open positions: 1" in out and "pt-2026-09-30-VST-exit" in out and "nothing changed" in out


@pytest.mark.unit
def test_send_turns_the_kill_switch_on_before_touching_the_account(capsys, settings):
    client = FlattenClient()
    assert flatten.main(["--send", "--reason", "test day"], env=PAPER_ENV, make_client=lambda env: client) == 0
    assert client.calls == [("cancel_orders", True), ("close_all_positions", True)]  # switch already on
    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert saved["risk"]["kill_switch"] is True and saved["risk"]["kill_switch_reason"] == "test day"
    assert saved["trading"] == {"days": ["mon"]}
    assert "kill switch ON" in capsys.readouterr().out


@pytest.mark.unit
def test_a_failed_cancel_still_closes_the_positions(settings):
    client = FlattenClient(fail_cancel=True)
    assert flatten.main(["--send"], env=PAPER_ENV, make_client=lambda env: client) == flatten.EXIT_PROBLEM
    assert ("close_all_positions", True) in client.calls


@pytest.mark.unit
def test_a_live_key_is_refused_and_nothing_changes(capsys, settings):
    code = flatten.main(["--send"], env={"ALPACA_API_KEY": "AKLIVE", "ALPACA_SECRET_KEY": "s"})
    assert code == flatten.EXIT_BAD_INPUT and "not a paper key" in capsys.readouterr().err
    assert guard.load_risk()["kill_switch"] is False


@pytest.mark.unit
def test_flatten_uses_the_paper_client():
    assert flatten.main.__defaults__[2] is sender.paper_client


@pytest.mark.unit
def test_a_flatten_is_written_to_the_ledger(tmp_path, monkeypatch, settings):
    from paper_orders import ledger

    monkeypatch.setenv("TRADINGAGENTS_UI_HOME", str(tmp_path))
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)
    flatten.main(["--send", "--reason", "drill"], env=PAPER_ENV, make_client=lambda env: FlattenClient())
    rows = ledger.read()
    assert [r["action"] for r in rows] == ["kill_switch_on", "cancel", "close"]
    assert rows[0]["reason"] == "drill" and rows[2]["symbol"] == "VST" and {r["mode"] for r in rows} == {"flatten"}
