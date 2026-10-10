"""The paper order sender loads the newest ticket file for a date, sends only its tickets, only to Alpaca paper,
refuses any key ID not starting with PK, and never sends one client order ID twice.

No test reaches Alpaca: a fake client stands in for the trading API.
"""

import json
from datetime import date
from pathlib import Path

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce

from paper_orders import sender
from paper_orders.sender import (
    NotPaperKey,
    TicketFileError,
    client_order_id,
    load_latest_tickets,
    order_request,
    paper_client,
    paper_keys,
    send_tickets,
)

AS_OF = date(2026, 9, 30)
PAPER_ENV = {"ALPACA_API_KEY": "PKTESTKEY000", "ALPACA_SECRET_KEY": "secret-do-not-print"}
VST = {"symbol": "VST", "side": "long", "order_side": "buy", "entry": 138.45, "stop": 137.28,
       "target": 147.72, "quantity": 36, "paper": True}
VIR = {"symbol": "VIR", "side": "short", "order_side": "sell", "entry": 11.21, "stop": 11.34,
       "target": 10.72, "quantity": 446, "paper": True}


def _file(tickets=None, created="2026-10-09T00:32:16+00:00", **over) -> dict:
    data = {"as_of": "2026-09-30", "created_at": created, "market_label": "SIDE",
            "tickets": [dict(t) for t in (tickets if tickets is not None else [VST, VIR])],
            "paper": True, "orders_placed": False, "ticket_version": 1}
    data.update(over)
    return data


def _write(folder: Path, stamp: str, data: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"paper_tickets_2026-09-30_{stamp}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class _HTTPError:
    def __init__(self, status):
        self.response = type("R", (), {"status_code": status})()


def _api_error(status: int, message: str) -> APIError:
    return APIError(json.dumps({"code": status * 1000, "message": message}), _HTTPError(status))


class FakeClient:
    """Holds orders by client order ID, like the paper account."""

    def __init__(self, reject=None, status="accepted"):
        self.orders: dict[str, dict] = {}
        self.submitted = []
        self.reject = reject or {}
        self.status = status

    def get_order_by_client_id(self, coid):
        if coid not in self.orders:
            raise _api_error(404, "order not found")
        return self.orders[coid]

    def submit_order(self, req):
        self.submitted.append(req)
        if req.symbol in self.reject:
            raise _api_error(403, self.reject[req.symbol])
        order = {"symbol": req.symbol, "qty": f"{req.qty:g}", "status": self.status, "filled_qty": "0",
                 "client_order_id": req.client_order_id}
        if self.status == "filled":
            order.update(filled_qty=f"{req.qty:g}", filled_avg_price=str(req.limit_price))
        self.orders[req.client_order_id] = order
        return order


# --- the ticket file ---------------------------------------------------------

@pytest.mark.unit
def test_loads_the_newest_file_by_created_at(tmp_path):
    _write(tmp_path, "20261008T235359Z", _file([VST], created="2026-10-08T23:53:59+00:00"))
    newest = _write(tmp_path, "20261009T003216Z", _file([VST, VIR]))
    path, data = load_latest_tickets(AS_OF, tmp_path)
    assert path == newest and [t["symbol"] for t in data["tickets"]] == ["VST", "VIR"]


@pytest.mark.unit
def test_no_file_for_the_date_is_refused(tmp_path):
    with pytest.raises(TicketFileError, match="no ticket file"):
        load_latest_tickets(AS_OF, tmp_path)


@pytest.mark.unit
@pytest.mark.parametrize("over, match", [
    ({"paper": False}, "not marked paper"),
    ({"orders_placed": True}, "orders_placed"),
    ({"market_label": "UP"}, "market label"),
    ({"ticket_version": 2}, "ticket_version"),
    ({"tickets": []}, "no tickets"),
    ({"tickets": [dict(VST, paper=False)]}, "VST is not marked paper"),
    ({"tickets": [dict(VST, order_side="sell")]}, "order side"),
    ({"tickets": [dict(VST, quantity=0)]}, "quantity"),
    ({"tickets": [dict(VST, quantity=1.5)]}, "quantity"),
    ({"tickets": [dict(VST, stop=140.0)]}, "either side of entry"),
    ({"tickets": [dict(VIR, target=12.0)]}, "either side of entry"),
    ({"tickets": [VST, VST]}, "twice"),
    ({"tickets": [dict(VST, symbol="vst")]}, "symbol"),
])
def test_a_bad_ticket_file_is_refused(tmp_path, over, match):
    _write(tmp_path, "20261009T003216Z", _file(**over))
    with pytest.raises(TicketFileError, match=match):
        load_latest_tickets(AS_OF, tmp_path)


# --- orders ------------------------------------------------------------------

@pytest.mark.unit
def test_each_ticket_is_one_bracket_limit_day_order_as_written():
    long_req, short_req = order_request(AS_OF, VST), order_request(AS_OF, VIR)
    assert (long_req.symbol, long_req.qty, long_req.side, long_req.limit_price) == ("VST", 36, OrderSide.BUY, 138.45)
    assert (long_req.take_profit.limit_price, long_req.stop_loss.stop_price) == (147.72, 137.28)
    assert (short_req.side, short_req.qty, short_req.limit_price) == (OrderSide.SELL, 446, 11.21)
    assert (short_req.take_profit.limit_price, short_req.stop_loss.stop_price) == (10.72, 11.34)
    for req in (long_req, short_req):
        assert req.order_class == OrderClass.BRACKET and req.time_in_force == TimeInForce.DAY
    assert long_req.client_order_id == "pt-2026-09-30-VST-buy"
    assert short_req.client_order_id == "pt-2026-09-30-VIR-sell"


# --- paper only --------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("key", ["AKLIVEKEY000", "pkLOWER", " ", ""])
def test_a_key_not_starting_with_pk_is_refused(key):
    with pytest.raises(NotPaperKey):
        paper_keys({"ALPACA_API_KEY": key, "ALPACA_SECRET_KEY": "s"})


@pytest.mark.unit
def test_the_refusal_names_no_secret():
    with pytest.raises(NotPaperKey) as info:
        paper_keys({"ALPACA_API_KEY": "AKLIVE", "ALPACA_SECRET_KEY": "secret-do-not-print"})
    assert "secret-do-not-print" not in str(info.value) and "AKLIVE" not in str(info.value)


@pytest.mark.unit
def test_the_client_points_at_the_paper_url():
    client = paper_client(PAPER_ENV)
    assert client._base_url == "https://paper-api.alpaca.markets"


@pytest.mark.unit
def test_the_cli_refuses_a_live_key_and_sends_nothing(tmp_path, capsys):
    _write(tmp_path, "20261009T003216Z", _file())
    code = sender.main(["--as-of", "2026-09-30", "--ticket-dir", str(tmp_path), "--send"],
                       env={"ALPACA_API_KEY": "AKLIVE", "ALPACA_SECRET_KEY": "s"})
    assert code == sender.EXIT_BAD_INPUT and "not a paper key" in capsys.readouterr().err


# --- sending -----------------------------------------------------------------

@pytest.mark.unit
def test_a_second_run_sends_nothing():
    client = FakeClient()
    first = send_tickets(client, AS_OF, [VST, VIR], wait=0)
    second = send_tickets(client, AS_OF, [VST, VIR], wait=0)
    assert [o.action for o in first] == ["sent", "sent"]
    assert [o.action for o in second] == ["already_sent", "already_sent"]
    assert len(client.submitted) == 2


@pytest.mark.unit
def test_a_rejection_is_reported_and_the_rest_still_go():
    client = FakeClient(reject={"VIR": "asset VIR is not shortable"})
    outcomes = send_tickets(client, AS_OF, [VST, VIR], wait=0)
    assert [o.action for o in outcomes] == ["sent", "rejected"]
    assert "not shortable" in outcomes[1].error and "403" in outcomes[1].error


@pytest.mark.unit
def test_a_failed_lookup_sends_nothing_for_that_ticket():
    client = FakeClient()
    client.get_order_by_client_id = lambda coid: (_ for _ in ()).throw(_api_error(500, "down"))
    outcomes = send_tickets(client, AS_OF, [VST], wait=0)
    assert outcomes[0].action == "lookup_failed" and client.submitted == []


@pytest.mark.unit
def test_the_cli_prints_fills_and_rejections(tmp_path, capsys):
    _write(tmp_path, "20261009T003216Z", _file())
    client = FakeClient(reject={"VIR": "insufficient buying power"}, status="filled")
    code = sender.main(["--as-of", "2026-09-30", "--ticket-dir", str(tmp_path), "--send", "--wait", "0"],
                       env=PAPER_ENV, make_client=lambda env: client)
    out = capsys.readouterr().out
    assert code == sender.EXIT_ORDER_PROBLEM
    assert "VST   sent      pt-2026-09-30-VST-buy: status filled, filled 36 of 36 @ 138.45" in out
    assert "VIR   REJECTED  pt-2026-09-30-VIR-sell: HTTP 403: insufficient buying power" in out


@pytest.mark.unit
def test_a_dry_run_builds_no_client(tmp_path, capsys):
    _write(tmp_path, "20261009T003216Z", _file())

    def no_client(env):
        raise AssertionError("a dry run must not build a client")

    code = sender.main(["--as-of", "2026-09-30", "--ticket-dir", str(tmp_path)], make_client=no_client)
    out = capsys.readouterr().out
    assert code == sender.EXIT_OK and "nothing sent" in out and client_order_id(AS_OF, VST) in out
