"""Reconcile: every problem type from a fake reader, a clean book, the report file, and GET-only reads."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from extensions.tradebot import store
from paper_orders import reconcile

NOW = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)  # 15:00 New York, a Tuesday
TODAY = "2026-11-03T14:40:00Z"
VST = {"symbol": "VST", "side": "long", "order_side": "buy", "quantity": 36}


class Reader:
    def __init__(self, positions=(), open_orders=(), recent=()):
        self._p, self._o, self._r = list(positions), list(open_orders), list(recent)
        self.asked_after = None

    def positions(self):
        return self._p

    def open_orders(self):
        return self._o

    def recent_orders(self, after):
        self.asked_after = after
        return self._r


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(store.HOME_ENV, str(tmp_path))
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "tickets"
    folder.mkdir()
    (folder / "paper_tickets_2026-11-02_x.json").write_text(json.dumps(
        {"as_of": "2026-11-02", "tickets": [VST]}), encoding="utf-8")
    return tmp_path


def _entry(status="filled", filled="36", coid="pt-2026-11-02-VST-buy", at=TODAY):
    return {"symbol": "VST", "client_order_id": coid, "order_class": "simple", "type": "limit", "status": status,
            "qty": "36", "filled_qty": filled, "filled_avg_price": "138.40", "submitted_at": at,
            "time_in_force": "day"}


def _exit(qty="36", coid="pt-2026-11-02-VST-exit", stop=True):
    legs = [{"symbol": "VST", "type": "stop", "stop_price": "137.23", "status": "held"}] if stop else []
    return {"symbol": "VST", "client_order_id": coid, "order_class": "oco", "type": "limit", "status": "new",
            "qty": qty, "filled_qty": "0", "legs": legs, "submitted_at": TODAY}


POSITION = {"symbol": "VST", "qty": "36", "side": "long"}


def _types(reader) -> list[str]:
    return sorted(p.type for p in reconcile.reconcile(reader, 10, NOW).problems)


@pytest.mark.unit
def test_a_filled_entry_with_its_exit_is_clean():
    assert _types(Reader([POSITION], [_exit()], [_entry(), _exit()])) == []


@pytest.mark.unit
def test_a_filled_entry_with_no_exit_is_reported():
    types = _types(Reader([POSITION], [], [_entry()]))
    assert "entry_filled_no_exit" in types and "position_no_stop" in types


@pytest.mark.unit
def test_an_exit_covering_the_wrong_quantity_is_reported():
    assert _types(Reader([POSITION], [_exit(qty="30")], [_entry(), _exit(qty="30")])) == ["exit_qty_mismatch"]


@pytest.mark.unit
def test_a_position_no_ticket_accounts_for_is_an_orphan():
    other = {**POSITION, "symbol": "ZZZ"}
    stop = {"symbol": "ZZZ", "type": "stop", "stop_price": "1", "order_class": "simple", "client_order_id": "x-exit",
            "qty": "36", "filled_qty": "0", "status": "new"}
    assert _types(Reader([other], [stop], [])) == ["orphan_position"]


@pytest.mark.unit
@pytest.mark.parametrize("at, now", [("2026-11-02T14:40:00Z", NOW),  # from an earlier session
                                     (TODAY, datetime(2026, 11, 3, 21, 5, tzinfo=UTC))])  # after the close
def test_an_open_entry_from_an_earlier_session_or_after_the_close_is_stale(at, now):
    entry = _entry(status="new", filled="0", at=at)
    problems = reconcile.reconcile(Reader([], [entry], [entry]), 10, now).problems
    assert [p.type for p in problems] == ["stale_open_entry"]


@pytest.mark.unit
def test_an_open_entry_during_its_session_is_fine():
    entry = _entry(status="new", filled="0")
    assert _types(Reader([], [entry], [entry])) == []


@pytest.mark.unit
def test_our_rejections_are_reported_and_others_are_not():
    ours = _entry(status="rejected", filled="0")
    theirs = {**ours, "client_order_id": "someone-else"}
    assert _types(Reader([], [], [ours, theirs])) == ["rejected"]


@pytest.mark.unit
def test_queue_statuses_are_checked_against_the_account(home):
    def item(ticker, status, **ticket):
        return {"id": ticker.lower() * 3, "ticker": ticker, "status": status,
                "ticket": {"client_order_id": f"tb-{ticker}-1-{ticker}-buy", "exit_order_id": f"tb-{ticker}-1-{ticker}-exit",
                           **ticket}}
    with store.transaction() as q:
        q["trade"] += [item("AAA", "sent"), item("BBB", "ticketed"), item("CCC", "sent", exit_done=True)]
    on_account = [{"symbol": "BBB", "client_order_id": "tb-BBB-1-BBB-buy", "status": "new", "order_class": "simple",
                   "submitted_at": TODAY},
                  {"symbol": "CCC", "client_order_id": "tb-CCC-1-CCC-buy", "status": "filled",
                   "order_class": "simple", "filled_qty": "1", "submitted_at": TODAY}]
    types = _types(Reader([], [], on_account))
    assert types == ["queue_exit_done_but_no_exit", "queue_sent_not_on_account", "queue_ticketed_but_on_account"]


@pytest.mark.unit
def test_a_full_page_of_history_is_flagged():
    orders = [{"client_order_id": f"o{i}", "status": "filled", "symbol": "X"} for i in range(500)]
    assert "history_truncated" in _types(Reader([], [], orders))


@pytest.mark.unit
def test_the_window_asks_the_account_for_the_last_n_days():
    reader = Reader()
    reconcile.reconcile(reader, 5, NOW)
    assert reader.asked_after == datetime(2026, 10, 29, 20, 0, tzinfo=UTC)


@pytest.mark.unit
def test_the_cli_saves_the_report_and_exits_three_on_problems(home, capsys):
    code = reconcile.main([], env={}, make_reader=lambda env: Reader([POSITION], [], [_entry()]), now=NOW)
    assert code == reconcile.EXIT_PROBLEMS
    path, report = reconcile.latest()
    assert path.name == "reconcile_20261103T200000Z.json" and report["clean"] is False
    assert report["by_type"]["entry_filled_no_exit"] == 1
    assert "entry_filled_no_exit" in capsys.readouterr().out


@pytest.mark.unit
def test_the_cli_exits_zero_when_clean_and_one_when_unreadable(home):
    clean = Reader([POSITION], [_exit()], [_entry(), _exit()])
    assert reconcile.main([], env={}, make_reader=lambda env: clean, now=NOW) == reconcile.EXIT_OK

    def broken(env):
        from paper_orders.snapshot import NotPaperKey
        raise NotPaperKey("no keys")

    assert reconcile.main([], env={}, make_reader=broken, now=NOW) == reconcile.EXIT_READ_FAILED


@pytest.mark.unit
def test_reconcile_loads_no_alpaca_module_and_names_no_order_call():
    out = subprocess.run([sys.executable, "-c", "import sys, paper_orders.reconcile; "
                          "print(sorted(m for m in sys.modules if m.startswith('alpaca')))"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
    source = Path(reconcile.__file__).read_text(encoding="utf-8")
    for word in ("submit_order", "cancel_order", "close_position", "replace_order", "POST", "DELETE"):
        assert word not in source, word


@pytest.mark.unit
def test_a_ticket_sent_days_after_its_date_still_counts(home):
    (home / "tickets" / "paper_tickets_2026-09-01_x.json").write_text(json.dumps(
        {"as_of": "2026-09-01", "tickets": [{**VST, "symbol": "OLD"}]}), encoding="utf-8")
    entry = {**_entry(coid="pt-2026-09-01-OLD-buy"), "symbol": "OLD"}
    report = reconcile.reconcile(Reader([{**POSITION, "symbol": "OLD"}], [], [entry]), 10, NOW)
    assert [p.type for p in report.problems] == ["position_no_stop", "entry_filled_no_exit"]
    assert report.counts["tickets_on_account"] == 1


@pytest.mark.unit
def test_a_tradebot_order_problem_names_its_queue_item():
    assert reconcile.Problem("rejected", "X", "d", "tb-0123456789ab-2-X-buy").item_id == "0123456789ab"
    assert reconcile.Problem("rejected", "X", "d", "pt-2026-11-02-X-buy").item_id is None
