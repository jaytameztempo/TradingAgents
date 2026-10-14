"""The paper snapshot reads open positions, open orders and the account, and has no way to place, cancel or
change an order.

Every test uses a fake reader or a fake opener; nothing here touches the network.
"""

import ast
import io
import json
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from paper_orders import snapshot
from paper_orders.snapshot import (
    ACCOUNT_PATH,
    EXIT_BAD_KEYS,
    EXIT_OK,
    EXIT_READ_FAILED,
    OPEN_ORDERS_PATH,
    PAPER_URL,
    POSITIONS_PATH,
    NotPaperKey,
    PaperReader,
    ReadFailed,
    main,
    paper_keys,
    paper_reader,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(snapshot.__file__)
PAPER_ENV = {"ALPACA_API_KEY": "PKTEST123", "ALPACA_SECRET_KEY": "secret"}

POSITION = {"symbol": "AAPL", "side": "long", "qty": "10", "avg_entry_price": "180.5",
            "current_price": "182.25", "market_value": "1822.5", "unrealized_pl": "17.5",
            "unrealized_plpc": "0.0096952908"}
ORDER = {"id": "o-1", "client_order_id": "pt-2026-10-09-MSFT-exit", "symbol": "MSFT", "side": "sell",
         "type": "limit", "qty": "5", "filled_qty": "0", "limit_price": "430", "stop_price": None,
         "time_in_force": "gtc", "status": "new", "order_class": "oco",
         "legs": [{"id": "o-2", "symbol": "MSFT", "side": "sell", "type": "stop", "qty": "5", "filled_qty": "0",
                   "limit_price": None, "stop_price": "410.5", "time_in_force": "gtc", "status": "held"}]}


class FakeReader:
    def __init__(self, positions=(), orders=()):
        self._positions, self._orders = list(positions), list(orders)

    def positions(self):
        return self._positions

    def open_orders(self):
        return self._orders


class FakeResponse(io.BytesIO):
    pass


class FakeOpener:
    """Stands in for urlopen: records each request and answers from a path -> body map."""

    def __init__(self, bodies=None, error=None):
        self.bodies, self.error, self.requests = bodies or {}, error, []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        return FakeResponse(json.dumps(self.bodies[req.full_url[len(PAPER_URL):]]).encode())


def _never_open(req, timeout=None):
    raise AssertionError(f"tried to open {req.full_url}")


# --- printing ----------------------------------------------------------------

@pytest.mark.unit
def test_prints_positions_and_open_orders_with_legs(capsys):
    assert main([], env=PAPER_ENV, make_reader=lambda env: FakeReader([POSITION], [ORDER])) == EXIT_OK
    out = capsys.readouterr().out
    assert "read-only snapshot" in out
    assert "open positions: 1" in out
    assert re.search(r"AAPL\s+long\s+qty\s+10\s+avg\s+180\.50\s+last\s+182\.25\s+value\s+1,822\.50\s+P/L \+17\.50 \(\+0\.97%\)", out)
    assert "open orders: 1" in out
    assert re.search(r"MSFT\s+sell\s+limit\s+qty 5, filled 0\s+limit 430\.00\s+gtc\s+new\s+oco\s+id pt-2026-10-09-MSFT-exit", out)
    assert re.search(r"leg MSFT\s+sell\s+stop\s+qty 5, filled 0\s+stop 410\.50\s+gtc\s+held", out)


@pytest.mark.unit
def test_an_empty_account_prints_none(capsys):
    assert main([], env=PAPER_ENV, make_reader=lambda env: FakeReader()) == EXIT_OK
    out = capsys.readouterr().out
    assert "open positions: 0\n  none" in out
    assert "open orders: 0\n  none" in out


@pytest.mark.unit
def test_the_reader_feeds_main_end_to_end_through_a_fake_opener(capsys):
    opener = FakeOpener({POSITIONS_PATH: [POSITION], OPEN_ORDERS_PATH: [ORDER]})
    assert main([], env=PAPER_ENV, make_reader=lambda env: paper_reader(env, opener=opener)) == EXIT_OK
    assert "AAPL" in capsys.readouterr().out
    assert len(opener.requests) == 2


# --- keys --------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("env, match", [
    ({"ALPACA_API_KEY": "AKLIVE123", "ALPACA_SECRET_KEY": "s"}, "not a paper key"),
    ({"ALPACA_API_KEY": "PKTEST123"}, "ALPACA_SECRET_KEY"),
    ({}, "ALPACA_API_KEY and ALPACA_SECRET_KEY"),
])
def test_live_or_missing_keys_are_refused_before_any_request(env, match, capsys):
    with pytest.raises(NotPaperKey, match=match):
        paper_keys(env)
    assert main([], env=env, make_reader=lambda e: paper_reader(e, opener=_never_open)) == EXIT_BAD_KEYS
    assert match in capsys.readouterr().err


# --- the reader can only GET three paths on the paper URL --------------------

@pytest.mark.unit
def test_the_reader_sends_only_gets_to_the_three_paper_paths():
    account = {"equity": "5000", "buying_power": "20000"}
    opener = FakeOpener({POSITIONS_PATH: [POSITION], OPEN_ORDERS_PATH: [ORDER], ACCOUNT_PATH: account})
    reader = PaperReader("PKTEST123", "secret", opener=opener)
    assert reader.positions() == [POSITION]
    assert reader.open_orders() == [ORDER]
    assert reader.account() == account
    assert [(r.get_method(), r.full_url, r.data) for r in opener.requests] == [
        ("GET", PAPER_URL + POSITIONS_PATH, None),
        ("GET", PAPER_URL + OPEN_ORDERS_PATH, None),
        ("GET", PAPER_URL + ACCOUNT_PATH, None),
    ]
    assert opener.requests[0].get_header("Apca-api-key-id") == "PKTEST123"
    assert opener.requests[0].get_header("Apca-api-secret-key") == "secret"


@pytest.mark.unit
def test_open_orders_asks_only_for_open_orders():
    assert OPEN_ORDERS_PATH.startswith("/v2/orders?")
    assert "status=open" in OPEN_ORDERS_PATH.split("?", 1)[1].split("&")


@pytest.mark.unit
def test_the_reader_exposes_only_three_read_methods():
    public = {name for name in dir(PaperReader) if not name.startswith("_")}
    assert public == {"positions", "open_orders", "account"}


@pytest.mark.unit
def test_paper_url_is_alpacas_paper_url():
    from alpaca.common.enums import BaseURL  # only the test imports alpaca, to check the hardcoded URL

    assert PAPER_URL == BaseURL.TRADING_PAPER.value


# --- read failures -----------------------------------------------------------

@pytest.mark.unit
def test_an_http_error_exits_with_a_readable_message(capsys):
    error = urllib.error.HTTPError(PAPER_URL + POSITIONS_PATH, 403, "Forbidden", {},
                                   io.BytesIO(b'{"code": 40310000, "message": "request is not authorized"}'))
    opener = FakeOpener(error=error)
    assert main([], env=PAPER_ENV, make_reader=lambda env: paper_reader(env, opener=opener)) == EXIT_READ_FAILED
    err = capsys.readouterr().err
    assert "HTTP 403" in err and "request is not authorized" in err


@pytest.mark.unit
def test_a_network_error_is_a_read_failure():
    reader = PaperReader("PKTEST123", "secret", opener=FakeOpener(error=urllib.error.URLError("no route")))
    with pytest.raises(ReadFailed, match="no route"):
        reader.positions()


@pytest.mark.unit
def test_a_response_that_is_not_a_list_is_a_read_failure():
    reader = PaperReader("PKTEST123", "secret", opener=FakeOpener({POSITIONS_PATH: {"message": "odd"}}))
    with pytest.raises(ReadFailed, match="expected a list"):
        reader.positions()


@pytest.mark.unit
def test_an_account_that_is_not_an_object_is_a_read_failure():
    reader = PaperReader("PKTEST123", "secret", opener=FakeOpener({ACCOUNT_PATH: []}))
    with pytest.raises(ReadFailed, match="expected an object"):
        reader.account()


# --- nothing that can send ---------------------------------------------------

@pytest.mark.unit
def test_the_module_imports_nothing_that_can_send():
    imported = set()
    for node in ast.walk(ast.parse(SOURCE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    banned = ("alpaca", "paper_orders", "ui", "extensions", "tradingagents")
    assert [m for m in imported if m.split(".")[0] in banned] == []


@pytest.mark.unit
def test_the_copied_key_names_still_match_alpaca_bars():
    from extensions.market_data import alpaca_bars  # only the test imports it, to check the copies

    assert snapshot.API_KEY_ENV == alpaca_bars.API_KEY_ENV == "ALPACA_API_KEY"
    assert snapshot.SECRET_KEY_ENV == alpaca_bars.SECRET_KEY_ENV == "ALPACA_SECRET_KEY"
    assert snapshot.PAPER_KEY_PREFIX == alpaca_bars.PAPER_KEY_PREFIX == "PK"


@pytest.mark.unit
def test_the_source_names_no_order_call_or_write_method():
    pattern = re.compile(r"submit_order|cancel_order|replace_order|close_position|close_all|OrderRequest"
                         r"|\b(POST|PUT|PATCH|DELETE)\b")
    offenders = [f"{n}: {line.strip()}"
                 for n, line in enumerate(SOURCE.read_text(encoding="utf-8").splitlines(), 1)
                 if pattern.search(line)]
    assert offenders == []


@pytest.mark.unit
def test_importing_the_snapshot_loads_no_trading_client_or_sender():
    code = ("import sys, paper_orders.snapshot; "
            "print(sorted(m for m in sys.modules if m.startswith(('alpaca.trading', 'paper_orders.sender', 'ui'))))")
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"
