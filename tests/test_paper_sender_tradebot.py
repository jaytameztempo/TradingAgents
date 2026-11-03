"""paper_orders.sender --tradebot: the queue's ticketed items, the guard in front of every entry, exits always
placed, and the queue updated (sent, blocked, armed again, last guard). A fake client stands in for Alpaca."""

import json
from datetime import date
from pathlib import Path

import pytest

from extensions.tradebot import store, ticketing
from paper_orders import sender
from tests.test_paper_order_sender import PAPER_ENV, FakeClient

AS_OF = "2026-09-30"
VST = {"symbol": "VST", "side": "long", "order_side": "buy", "entry": 138.45, "stop": 137.28, "stop_offset": 1.17,
       "target": 147.72, "quantity": 36, "paper": True, "risk_dollars": 42.12}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(store.HOME_ENV, str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    return tmp_path


def _queue(home, bot="UP", ticket=VST, status="ticketed", file="tradebot_tickets_2026-09-30_x.json",
           expires="2099-01-01", **ticket_over):
    item = store.new_item(ticker=ticket["symbol"], as_of=AS_OF, kind="up_cat", scan="s", scan_file="f.json",
                          side=ticket["side"], needs_regime=bot, metrics={}, source="auto")
    prefix = ticketing.id_prefix(item["id"], 1)
    t = {**ticket, "item_id": item["id"], "bot": bot, "attempt": 1, "id_prefix": prefix,
         "client_order_id": ticketing.entry_order_id(item["id"], 1, ticket["symbol"], ticket["side"]),
         "exit_order_id": ticketing.exit_order_id(item["id"], 1, ticket["symbol"]), **ticket_over}
    folder = ticketing.tickets_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / file
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "kind": "tradebot", "tradebot_ticket_version": ticketing.TRADEBOT_TICKET_VERSION, "paper": True,
        "orders_placed": False, "as_of": AS_OF, "tickets": []}
    data["tickets"].append(t)
    path.write_text(json.dumps(data), encoding="utf-8")
    store.enqueue("trade", [item])
    with store.transaction() as q:
        it = store.find(q, item["id"])
        it["attempts"] = 1
        it["expires_after"] = expires
        it["ticket"] = {"file": file, "id_prefix": prefix, "attempt": 1, "client_order_id": t["client_order_id"],
                        "exit_order_id": t["exit_order_id"], "risk_dollars": t["risk_dollars"]}
        store.set_status(it, status, "test")
    return item["id"], t


def _main(args, client, **kw):
    return sender.main(["--tradebot", *args, "--wait", "0"], env=PAPER_ENV, make_client=lambda env: client, **kw)


def _item(item_id):
    return store.find(store.read(), item_id)


@pytest.mark.unit
def test_a_ticketed_item_is_sent_once_and_marked_sent(home, capsys):
    item_id, t = _queue(home)
    client = FakeClient()
    assert _main(["--send"], client) == sender.EXIT_OK
    assert [r.client_order_id for r in client.submitted] == [t["client_order_id"]]
    assert t["client_order_id"] == f"tb-{item_id}-1-VST-buy"
    item = _item(item_id)
    assert item["status"] == "sent" and item["last_guard"]["decision"] == "allowed"
    out = capsys.readouterr().out
    assert "guard: equity 100,000.00" in out and "VST   allowed" in out


@pytest.mark.unit
def test_an_exit_goes_on_a_later_run_from_the_fill_then_the_item_is_done(home):
    item_id, t = _queue(home)
    client = FakeClient()
    _main(["--send"], client)
    client.fill(t["client_order_id"], 36, 138.40)
    assert _main(["--send"], client) == sender.EXIT_OK
    [exit_req] = client.exits()
    assert exit_req.client_order_id == t["exit_order_id"] and exit_req.stop_loss.stop_price == 137.23
    assert _item(item_id)["ticket"]["exit_done"] is True
    assert sender.load_tradebot() == []  # nothing left to do for it


@pytest.mark.unit
def test_an_unfilled_day_entry_arms_the_item_again(home, capsys):
    item_id, t = _queue(home)
    client = FakeClient()
    _main(["--send"], client)
    client.orders[t["client_order_id"]]["status"] = "expired"
    _main(["--send"], client)
    item = _item(item_id)
    assert item["status"] == "armed" and item["ticket"] is None and len(item["past_tickets"]) == 1
    assert "armed again" in capsys.readouterr().out


@pytest.mark.unit
def test_a_rejected_entry_blocks_the_item(home):
    item_id, _ = _queue(home)
    assert _main(["--send"], FakeClient(reject={"VST": "insufficient buying power"})) == sender.EXIT_ORDER_PROBLEM
    item = _item(item_id)
    assert item["status"] == "blocked" and "insufficient buying power" in item["status_reason"]


@pytest.mark.unit
def test_the_kill_switch_holds_the_entry_and_records_why(home, capsys):
    (home / "settings.json").write_text(json.dumps({"risk": {"kill_switch": True, "kill_switch_reason": "test"}}),
                                        encoding="utf-8")
    item_id, _ = _queue(home)
    client = FakeClient()
    assert _main(["--send"], client) == sender.EXIT_GUARD_BLOCKED
    assert client.submitted == []
    item = _item(item_id)
    assert item["status"] == "ticketed" and item["last_guard"]["decision"] == "blocked"
    assert "kill switch is on" in item["last_guard"]["reason"]
    assert "held back by the risk guard" in capsys.readouterr().out


@pytest.mark.unit
def test_exits_are_placed_even_with_the_kill_switch_on(home):
    item_id, t = _queue(home)
    client = FakeClient()
    _main(["--send"], client)
    client.fill(t["client_order_id"], 36, 138.45)
    (home / "settings.json").write_text(json.dumps({"risk": {"kill_switch": True}}), encoding="utf-8")
    _main(["--send"], client)
    assert [r.client_order_id for r in client.exits()] == [t["exit_order_id"]]


@pytest.mark.unit
def test_a_broken_risk_setting_blocks_entries(home):
    (home / "settings.json").write_text(json.dumps({"risk": {"daily_loss_pct": "three"}}), encoding="utf-8")
    item_id, _ = _queue(home)
    client = FakeClient()
    assert _main(["--send"], client) == sender.EXIT_GUARD_BLOCKED and client.submitted == []
    assert "risk settings" in _item(item_id)["last_guard"]["reason"]


@pytest.mark.unit
@pytest.mark.parametrize("bot, side_ticket", [("UP", {**VST, "side": "short", "order_side": "sell", "stop": 139.62,
                                                       "target": 130.0}),
                                               ("DOWN", VST)])
def test_an_item_on_the_wrong_side_for_its_bot_is_refused(home, capsys, bot, side_ticket):
    _queue(home, bot=bot, ticket=side_ticket)
    client = FakeClient()
    assert _main(["--send"], client) == sender.EXIT_BAD_INPUT and client.submitted == []
    assert "may not be" in capsys.readouterr().err


@pytest.mark.unit
def test_a_ticket_whose_id_does_not_match_its_prefix_is_refused(home, capsys):
    _queue(home, client_order_id="tb-someoneelse-1-VST-buy")
    assert _main(["--send"], FakeClient()) == sender.EXIT_BAD_INPUT


@pytest.mark.unit
def test_preview_reads_the_account_and_writes_nothing(home, capsys):
    item_id, _ = _queue(home)
    client = FakeClient()
    assert _main(["--preview"], client) == sender.EXIT_OK
    assert client.submitted == []
    assert _item(item_id)["status"] == "ticketed" and _item(item_id)["last_guard"] is None
    assert not (home / "risk_state.json").exists()
    out = capsys.readouterr().out
    assert "VST   allowed" in out and "preview: nothing sent" in out


@pytest.mark.unit
def test_a_dry_run_builds_no_client(home, capsys):
    _queue(home)

    def no_client(env):
        raise AssertionError("a dry run must not build a client")

    assert sender.main(["--tradebot"], make_client=no_client) == sender.EXIT_OK
    assert "dry run: nothing sent" in capsys.readouterr().out


@pytest.mark.unit
def test_the_daily_and_manual_modes_go_through_the_guard_too(home, tmp_path, capsys):
    from tests.test_paper_order_sender import _file, _write

    tickets = tmp_path / "tickets"
    _write(tickets, "20261009T003216Z", _file())
    (home / "settings.json").write_text(json.dumps({"risk": {"kill_switch": True}}), encoding="utf-8")
    client = FakeClient()
    code = sender.main(["--as-of", AS_OF, "--ticket-dir", str(tickets), "--send", "--wait", "0"], env=PAPER_ENV,
                       make_client=lambda env: client)
    assert code == sender.EXIT_GUARD_BLOCKED and client.submitted == []


@pytest.mark.unit
def test_the_outcome_hook_gets_every_send(home):
    _queue(home)
    seen = []
    _main(["--send"], FakeClient(), on_outcomes=lambda mode, e, x, d: seen.append((mode, len(e), d.blocked)))
    assert seen == [("tradebot", 1, None)]


@pytest.mark.unit
def test_the_peak_equity_is_saved_on_a_send(home):
    _queue(home)
    _main(["--send"], FakeClient(account={"equity": "101000", "last_equity": "100000", "buying_power": "1"}))
    assert json.loads((home / "risk_state.json").read_text(encoding="utf-8"))["peak_equity"] == 101000.0


@pytest.mark.unit
def test_ids_match_the_sender_format(home):
    item_id, t = _queue(home)
    assert sender.client_order_id(date(2026, 9, 30), t, t["id_prefix"]) == t["client_order_id"]
    assert sender.exit_order_id(date(2026, 9, 30), t, t["id_prefix"]) == t["exit_order_id"]
    assert Path(ticketing.tickets_dir()).is_relative_to(home)


@pytest.mark.unit
def test_an_unfilled_entry_past_the_items_last_session_expires_it(home):
    item_id, t = _queue(home, expires="2026-10-14")
    client = FakeClient()
    _main(["--send"], client)
    client.orders[t["client_order_id"]]["status"] = "expired"
    _main(["--send"], client)
    assert _item(item_id)["status"] == "expired"
