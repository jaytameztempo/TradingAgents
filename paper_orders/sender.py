"""Send one date's saved paper tickets to the Alpaca paper account, and nowhere else.

Run from the TradingAgents folder:

    python -m paper_orders.sender --as-of 2026-09-30           # print the orders, send nothing
    python -m paper_orders.sender --as-of 2026-09-30 --send    # send them to Alpaca paper
    python -m paper_orders.sender --manual manual_ticket_2026-09-30_VST_0123456789ab.json --send

It loads the newest ``paper_tickets_<as_of>_*.json`` (by the created_at inside
each file) from ~/.tradingagents/tickets/ unless --ticket-dir is set, and sends
only the tickets in that file, in two steps:

- entry: one plain limit DAY order at the ticket's entry for the ticket's
  quantity, with no stop or take-profit attached. A limit can fill better than
  its price, so a stop fixed before the fill could sit on the wrong side of it.
- exits: on a --send run after the entry is done filling (filled, or ended with
  some shares filled), one OCO GTC order for the filled shares: take-profit at
  the ticket's target and stop at ``stop_for_fill`` of the actual average fill.
  Nothing is placed while the entry is still open or partly filled, and no
  exits are placed for an entry that is already a bracket order.

Nothing is resized or added. The same --send run that sends the entries places
the exits for any entry that already filled; run it again later for the rest.

Paper only:

- the key ID in ALPACA_API_KEY must start with "PK"; any other key is refused
  before a client is built,
- the client is always ``TradingClient(..., paper=True)`` with no URL override,
  and its base URL is checked against Alpaca's paper URL before any request.

Each entry carries a client order ID, ``pt-<as_of>-<SYMBOL>-<buy|sell>``, and
each exit ``pt-<as_of>-<SYMBOL>-exit``; a manual ticket's IDs start ``mt-<as_of>-<file id>``
instead. Before sending either, the paper
account is asked for that ID; if an order already has it, its status is printed
and nothing is sent, so a second run never double-sends.
A --send outside the operator's trading window (``paper_orders.trade_window``, set on the UI's Settings
page) sends nothing; with no window saved, nothing is gated. A dry run is allowed at any time.
The ticket files, scans, router and bots are not touched.

A manual ticket (``--manual NAME``) is one operator-written ticket in ``<ticket dir>/manual/``,
named ``manual_ticket_<as_of>_<SYMBOL>_<id>.json`` (the UI's Trade page writes these). It gets the
same per-ticket checks, entry and exits as the daily file, but needs no SIDE market label, so trend
names can be sent too.

--tradebot sends the Tradebot queue's ticketed items (extensions.tradebot): each item's ticket from its
tradebot_tickets file, checked like a daily ticket, an UP item long only and a DOWN item short only, with IDs
``tb-<item>-<attempt>-<SYMBOL>-<buy|sell|exit>``. It also places exits for items already sent. The queue is
updated under its lock: sent, blocked when rejected, armed again (or expired) when a DAY entry ended unfilled,
and each item's last guard decision.

Every --send, in every mode, puts new entries through the risk guard (``paper_orders.guard``: kill switch,
daily loss, drawdown, already held, position and gross exposure limits, book stop-risk) first. Exits are never
held back: they are placed before and after the entries. --preview reads the account (GET only) and prints the
guard's decisions, sending nothing; a plain dry run builds no client at all. From the command line (and so
from the UI and the scheduler) every --send is logged to the order ledger (``paper_orders.ledger``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from alpaca.common.enums import BaseURL
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)
from dotenv import load_dotenv

from extensions.market_data.alpaca_bars import (
    API_KEY_ENV,
    PAPER_KEY_PREFIX,
    SECRET_KEY_ENV,
    validate_ticker,
)
from extensions.status.daily_status import DOWN, LONG, SHORT, SIDE, UP
from extensions.status.paper_tickets import TICKET_VERSION, default_ticket_dir, stop_for_fill
from extensions.tradebot import store
from extensions.tradebot.ticketing import (
    TRADEBOT_TICKET_VERSION,
    tickets_dir as tradebot_tickets_dir,
)
from paper_orders import guard, ledger, risk_state
from paper_orders.trade_window import SettingsError, load_window

REPO_ROOT = Path(__file__).resolve().parents[1]
PAPER_URL = BaseURL.TRADING_PAPER.value
ORDER_SIDE = {LONG: "buy", SHORT: "sell"}
FINAL = {"filled", "rejected", "canceled", "expired", "done_for_day", "replaced"}
EXIT_OK, EXIT_BAD_INPUT, EXIT_ORDER_PROBLEM, EXIT_GUARD_BLOCKED = 0, 2, 3, 4
BOT_SIDE = {UP: LONG, DOWN: SHORT}  # SIDE items may be either
PROBLEMS = {"rejected", "lookup_failed", "exit_rejected", "exit_refused", "exit_lookup_failed"}
MANUAL_DIR = "manual"
MANUAL_VERSION = 1
MANUAL_FILE = re.compile(r"^manual_ticket_(\d{4}-\d{2}-\d{2})_([A-Z]{1,5}(?:[.-][A-Z]{1,2})?)_([0-9a-f]{12})\.json$")


class TicketFileError(RuntimeError):
    """The ticket file is missing, unreadable, or not a set of paper tickets this sender will send."""


class NotPaperKey(RuntimeError):
    """The Alpaca keys are missing, or the key ID is not a paper key."""


# --- the ticket file ---------------------------------------------------------

def load_latest_tickets(as_of: date, ticket_dir: str | Path | None = None) -> tuple[Path, dict]:
    """The newest ticket file for as_of, by created_at, checked ticket by ticket."""
    folder = Path(ticket_dir) if ticket_dir is not None else default_ticket_dir()
    found = []
    for path in sorted(folder.glob(f"paper_tickets_{as_of}_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(data["created_at"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TicketFileError(f"cannot read ticket file {path}: {exc}") from exc
        found.append((created, path, data))
    if not found:
        raise TicketFileError(f"no ticket file for {as_of} in {folder}")
    _, path, data = max(found, key=lambda item: item[0])
    _check_file(as_of, path, data)
    return path, data


def _check_file(as_of: date, path: Path, data: dict) -> None:
    def bad(why: str) -> TicketFileError:
        return TicketFileError(f"ticket file {path.name}: {why}")

    if data.get("as_of") != as_of.isoformat():
        raise bad(f"is for {data.get('as_of')}, not {as_of}")
    if data.get("ticket_version") != TICKET_VERSION:
        raise bad(f"has unsupported ticket_version {data.get('ticket_version')!r}")
    if data.get("paper") is not True:
        raise bad("is not marked paper")
    if data.get("orders_placed") is not False:
        raise bad("is not marked orders_placed false")
    if data.get("market_label") != SIDE:
        raise bad(f"has market label {data.get('market_label')}, not {SIDE}")
    tickets = data.get("tickets")
    if not isinstance(tickets, list) or not tickets:
        raise bad("has no tickets")
    seen = set()
    for t in tickets:
        _check_ticket(t, bad)
        if t["symbol"] in seen:
            raise bad(f"{t['symbol']} is ticketed twice")
        seen.add(t["symbol"])


def _check_ticket(t: dict, bad: Callable[[str], TicketFileError]) -> None:
    """One ticket: a plain symbol, paper, a long buy or short sell, a whole quantity, and its prices in order."""
    symbol = t.get("symbol")
    try:
        if validate_ticker(symbol) != symbol:
            raise ValueError("not upper-case and trimmed")
    except ValueError as exc:
        raise bad(f"ticket symbol {symbol!r}: {exc}") from exc
    if t.get("paper") is not True:
        raise bad(f"{symbol} is not marked paper")
    side = t.get("side")
    if side not in ORDER_SIDE or t.get("order_side") != ORDER_SIDE[side]:
        raise bad(f"{symbol} has side {side!r} and order side {t.get('order_side')!r}")
    qty = t.get("quantity")
    if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
        raise bad(f"{symbol} has quantity {qty!r}, not a whole number of at least 1")
    entry, stop, target, offset = t.get("entry"), t.get("stop"), t.get("target"), t.get("stop_offset")
    if not all(_is_price(v) for v in (entry, stop, target, offset)):
        raise bad(f"{symbol} has entry {entry!r}, stop {stop!r}, target {target!r}, stop_offset {offset!r}")
    sign = 1 if side == LONG else -1
    if not sign * (entry - stop) > 0 or not sign * (target - entry) > 0:
        raise bad(f"{symbol} {side}: stop {stop} and target {target} are not on either side of entry {entry}")
    try:
        planned = stop_for_fill(side, offset, entry)
    except ValueError as exc:
        raise bad(f"{symbol}: {exc}") from exc
    if planned != stop:
        raise bad(f"{symbol}: stop {stop} is not stop_offset {offset} from entry {entry} ({planned})")


def load_manual(name: str, ticket_dir: str | Path | None = None) -> tuple[Path, dict]:
    """One manual ticket file by name, from the manual folder only, checked like a daily ticket."""
    m = MANUAL_FILE.fullmatch(name or "")
    if not m:
        raise TicketFileError(f"{name!r} is not a manual ticket file name (manual_ticket_<as_of>_<SYMBOL>_<id>.json)")
    path = (Path(ticket_dir) if ticket_dir is not None else default_ticket_dir()) / MANUAL_DIR / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TicketFileError(f"cannot read manual ticket {path}: {exc}") from exc

    def bad(why: str) -> TicketFileError:
        return TicketFileError(f"manual ticket {name}: {why}")

    if not isinstance(data, dict) or data.get("kind") != "manual":
        raise bad("is not marked kind manual")
    if data.get("manual_version") != MANUAL_VERSION:
        raise bad(f"has unsupported manual_version {data.get('manual_version')!r}")
    if data.get("as_of") != m[1] or data.get("id") != m[3]:
        raise bad(f"has as_of {data.get('as_of')!r} and id {data.get('id')!r}, not the {m[1]} and {m[3]} in its name")
    if data.get("paper") is not True:
        raise bad("is not marked paper")
    if data.get("orders_placed") is not False:
        raise bad("is not marked orders_placed false")
    tickets = data.get("tickets")
    if not isinstance(tickets, list) or len(tickets) != 1 or not isinstance(tickets[0], dict):
        raise bad("must hold exactly one ticket")
    _check_ticket(tickets[0], bad)
    if tickets[0]["symbol"] != m[2]:
        raise bad(f"ticket symbol {tickets[0]['symbol']} is not the {m[2]} in its name")
    return path, data


def manual_prefix(data: dict) -> str:
    """The client order ID prefix for a manual ticket: its date and its own file id."""
    return f"mt-{data['as_of']}-{data['id']}"

def _is_price(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and v > 0


# --- orders ------------------------------------------------------------------

def _prefix(as_of: date, id_prefix: str | None) -> str:
    return id_prefix or f"pt-{as_of}"


def client_order_id(as_of: date, ticket: dict, id_prefix: str | None = None) -> str:
    """The entry's ID, the same on every run for one date (or manual ticket), symbol and side."""
    return f"{_prefix(as_of, id_prefix)}-{ticket['symbol']}-{ticket['order_side']}"


def exit_order_id(as_of: date, ticket: dict, id_prefix: str | None = None) -> str:
    """The exit pair's ID, the same on every run for one date (or manual ticket) and symbol."""
    return f"{_prefix(as_of, id_prefix)}-{ticket['symbol']}-exit"


def entry_request(as_of: date, ticket: dict, id_prefix: str | None = None) -> LimitOrderRequest:
    """One plain limit DAY order from one ticket, entry and size as written, nothing attached."""
    return LimitOrderRequest(
        symbol=ticket["symbol"],
        qty=ticket["quantity"],
        side=OrderSide.BUY if ticket["order_side"] == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=ticket["entry"],
        order_class=OrderClass.SIMPLE,
        client_order_id=client_order_id(as_of, ticket, id_prefix),
    )


@dataclass(frozen=True)
class Fill:
    qty: int
    price: float


def entry_fill(ticket: dict, order: dict) -> Fill | tuple[str, str]:
    """The finished fill of an entry order, or (action, why) when there is none to place exits for."""
    status = order.get("status")
    if (order.get("order_class") or "simple") != "simple" or order.get("legs"):
        return "exit_refused", f"entry is a {order.get('order_class')} order with its own exits"
    try:
        qty = float(order.get("filled_qty") or 0)
    except (TypeError, ValueError):
        return "exit_refused", f"filled_qty {order.get('filled_qty')!r} is not a number"
    if status not in FINAL:
        return "exit_waiting", f"entry is {status}, filled {qty:g} of {order.get('qty')}"
    if qty <= 0:
        return "no_fill", f"entry is {status} with nothing filled"
    if qty != int(qty) or not 1 <= qty <= ticket["quantity"]:
        return "exit_refused", f"filled_qty {qty:g} is not a whole number from 1 to {ticket['quantity']}"
    try:
        price = float(order.get("filled_avg_price"))
    except (TypeError, ValueError):
        price = 0.0
    if not price > 0:
        return "exit_refused", f"filled_avg_price {order.get('filled_avg_price')!r} is not a price"
    return Fill(int(qty), price)


def exit_request(as_of: date, ticket: dict, fill: Fill, id_prefix: str | None = None) -> LimitOrderRequest:
    """One OCO GTC exit for the filled shares: take-profit at the target, stop at stop_for_fill of the fill."""
    stop = stop_for_fill(ticket["side"], ticket["stop_offset"], fill.price)
    sign = 1 if ticket["side"] == LONG else -1
    if not sign * (ticket["target"] - fill.price) > 0:
        raise ValueError(f"target {ticket['target']} is not on the profit side of fill {fill.price}")
    return LimitOrderRequest(
        symbol=ticket["symbol"],
        qty=fill.qty,
        side=OrderSide.SELL if ticket["order_side"] == "buy" else OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.OCO,
        take_profit=TakeProfitRequest(limit_price=ticket["target"]),
        stop_loss=StopLossRequest(stop_price=stop),
        client_order_id=exit_order_id(as_of, ticket, id_prefix),
    )


# --- the paper client --------------------------------------------------------

def paper_keys(env: Mapping[str, str]) -> tuple[str, str]:
    """The keys, refused unless both are set and the key ID starts with PK."""
    api_key = (env.get(API_KEY_ENV) or "").strip()
    secret_key = (env.get(SECRET_KEY_ENV) or "").strip()
    missing = [n for n, v in ((API_KEY_ENV, api_key), (SECRET_KEY_ENV, secret_key)) if not v]
    if missing:
        raise NotPaperKey(f"set {' and '.join(missing)} to your Alpaca paper keys in .env")
    if not api_key.startswith(PAPER_KEY_PREFIX):
        raise NotPaperKey(f"{API_KEY_ENV} is not a paper key (paper key IDs start with {PAPER_KEY_PREFIX!r}); "
                          "this sender sends to the paper account only")
    return api_key, secret_key


def paper_client(env: Mapping[str, str]) -> TradingClient:
    """Alpaca's trading client on the paper URL. There is no live path."""
    api_key, secret_key = paper_keys(env)
    client = TradingClient(api_key, secret_key, paper=True, raw_data=True)
    if client._base_url != PAPER_URL:
        raise NotPaperKey(f"the trading client points at {client._base_url}, not {PAPER_URL}")
    return client


# --- sending -----------------------------------------------------------------

@dataclass
class Outcome:
    symbol: str
    client_order_id: str
    # entries: sent, already_sent, rejected, lookup_failed
    # exits: exit_sent, exit_already_sent, exit_rejected, exit_refused, exit_lookup_failed,
    #        exit_waiting (entry still open), no_fill, no_entry
    action: str
    order: dict | None = None
    error: str | None = None
    fill: Fill | None = None
    stop: float | None = None


def _error_text(exc: APIError) -> str:
    try:
        text = exc.message
    except (ValueError, TypeError, KeyError):
        text = str(exc)
    return f"HTTP {exc.status_code}: {text}" if exc.status_code else text


def _existing(client: Any, coid: str) -> dict | None:
    """The order with this client order ID, or None when the paper account has none."""
    try:
        return client.get_order_by_client_id(coid)
    except APIError as exc:
        if exc.status_code == 404:
            return None
        raise


def _settle(client: Any, coid: str, order: dict, wait: float, sleep: Callable[[float], None]) -> dict:
    """Re-read the order until it is final or wait seconds pass, so a quick fill or rejection shows."""
    deadline = time.monotonic() + wait
    while order.get("status") not in FINAL and time.monotonic() < deadline:
        sleep(0.5)
        try:
            order = client.get_order_by_client_id(coid)
        except APIError:
            break
    return order


def send_tickets(client: Any, as_of: date, tickets: list[dict], wait: float = 5.0,
                 sleep: Callable[[float], None] = time.sleep, id_prefix: str | None = None) -> list[Outcome]:
    """Send each ticket's entry once. An entry whose client order ID is already on the account is not sent again."""
    outcomes = []
    for t in tickets:
        coid = client_order_id(as_of, t, id_prefix)
        try:
            found = _existing(client, coid)
        except APIError as exc:
            outcomes.append(Outcome(t["symbol"], coid, "lookup_failed", error=_error_text(exc)))
            continue
        if found is not None:
            outcomes.append(Outcome(t["symbol"], coid, "already_sent", order=found))
            continue
        try:
            order = client.submit_order(entry_request(as_of, t, id_prefix))
        except APIError as exc:
            outcomes.append(Outcome(t["symbol"], coid, "rejected", error=_error_text(exc)))
            continue
        order = _settle(client, coid, order, wait, sleep)
        action = "rejected" if order.get("status") == "rejected" else "sent"
        outcomes.append(Outcome(t["symbol"], coid, action, order=order))
    return outcomes


def place_exits(client: Any, as_of: date, tickets: list[dict], id_prefix: str | None = None) -> list[Outcome]:
    """For each entry done filling, send its OCO exit once, the stop measured from the actual fill."""
    outcomes = []
    for t in tickets:
        entry_id, coid = client_order_id(as_of, t, id_prefix), exit_order_id(as_of, t, id_prefix)
        try:
            entry = _existing(client, entry_id)
        except APIError as exc:
            outcomes.append(Outcome(t["symbol"], coid, "exit_lookup_failed", error=_error_text(exc)))
            continue
        if entry is None:
            outcomes.append(Outcome(t["symbol"], coid, "no_entry", error=f"{entry_id} is not on the account"))
            continue
        fill = entry_fill(t, entry)
        if not isinstance(fill, Fill):
            action, why = fill
            outcomes.append(Outcome(t["symbol"], coid, action, order=entry, error=why))
            continue
        try:
            found = _existing(client, coid)
        except APIError as exc:
            outcomes.append(Outcome(t["symbol"], coid, "exit_lookup_failed", error=_error_text(exc), fill=fill))
            continue
        if found is not None:
            outcomes.append(Outcome(t["symbol"], coid, "exit_already_sent", order=found, fill=fill))
            continue
        try:
            req = exit_request(as_of, t, fill, id_prefix)
        except ValueError as exc:
            outcomes.append(Outcome(t["symbol"], coid, "exit_refused", error=str(exc), fill=fill))
            continue
        stop = req.stop_loss.stop_price
        try:
            order = client.submit_order(req)
        except APIError as exc:
            outcomes.append(Outcome(t["symbol"], coid, "exit_rejected", error=_error_text(exc), fill=fill, stop=stop))
            continue
        action = "exit_rejected" if order.get("status") == "rejected" else "exit_sent"
        outcomes.append(Outcome(t["symbol"], coid, action, order=order, fill=fill, stop=stop))
    return outcomes


# --- the Tradebot queue ----------------------------------------------------------

@dataclass
class QueuedTicket:
    item_id: str
    status: str  # ticketed: entry and exits; sent: exits only
    as_of: date
    prefix: str
    ticket: dict
    expires_after: str | None


def load_tradebot(item_ids: set[str] | None = None, folder: Path | None = None) -> list[QueuedTicket]:
    """The queue's ticketed items, and its sent items still waiting for exits, each with its ticket from its
    tradebot_tickets file, checked like a daily ticket. Any ticket that fails a check stops the run."""
    folder = folder or tradebot_tickets_dir()
    try:
        items = store.read()["trade"]
    except store.StoreError as exc:
        raise TicketFileError(str(exc)) from exc
    out, files = [], {}
    for item in items:
        t = item.get("ticket") or {}
        if item.get("status") not in ("ticketed", "sent") or not t.get("file") or t.get("exit_done"):
            continue
        if item_ids and item["id"] not in item_ids:
            continue
        name = t["file"]

        def bad(why: str, name=name, item=item) -> TicketFileError:
            return TicketFileError(f"tradebot ticket {name} for {item['ticker']} ({item['id']}): {why}")

        if name not in files:
            path = folder / name
            try:
                files[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise bad(f"cannot read {path}: {exc}") from exc
        data = files[name]
        if data.get("kind") != "tradebot" or data.get("tradebot_ticket_version") != TRADEBOT_TICKET_VERSION:
            raise bad("is not a tradebot ticket file this sender knows")
        if data.get("paper") is not True or data.get("orders_placed") is not False:
            raise bad("is not marked paper with orders_placed false")
        ticket = next((x for x in data.get("tickets") or [] if x.get("item_id") == item["id"]
                       and x.get("client_order_id") == t.get("client_order_id")), None)
        if ticket is None:
            raise bad(f"has no ticket {t.get('client_order_id')}")
        _check_ticket(ticket, bad)
        if ticket["symbol"] != item["ticker"]:
            raise bad(f"names {ticket['symbol']}, not the queued {item['ticker']}")
        want = BOT_SIDE.get(item.get("bot"))
        if item.get("bot") not in (UP, DOWN, SIDE) or (want and ticket["side"] != want):
            raise bad(f"a {item.get('bot')} item may not be {ticket['side']}")
        prefix = ticket.get("id_prefix") or ""
        if not prefix.startswith(f"tb-{item['id']}-") or client_order_id(date.min, ticket, prefix) != \
                ticket["client_order_id"]:
            raise bad(f"client order ID {ticket['client_order_id']} does not match its prefix {prefix!r}")
        out.append(QueuedTicket(item["id"], item["status"], date.fromisoformat(data["as_of"]), prefix, ticket,
                                item.get("expires_after")))
    return out


def read_book(client: Any) -> tuple[dict, list[dict], list[dict]]:
    """The account, open positions and open orders, as the guard reads them. GET requests only."""
    account = client.get_account()
    positions = client.get_all_positions()
    orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=500))
    return dict(account), list(positions), list(orders)


def guard_entries(client: Any, tickets: list[dict], coids: dict[str, str], now: datetime | None = None,
                  save_state: bool = True) -> guard.Decision:
    """The guard's decision on the tickets whose entries are not on the account yet. Anything it cannot read
    or check blocks every entry."""
    decision = guard.Decision()

    def blocked(why: str) -> guard.Decision:
        decision.blocked = why
        decision.skipped = {t["symbol"]: why for t in tickets}
        return decision

    try:
        risk = guard.load_risk()
    except SettingsError as exc:
        return blocked(f"risk settings: {exc}")
    try:
        state = risk_state.load()
    except risk_state.RiskStateError as exc:
        return blocked(str(exc))
    try:
        account, positions, orders = read_book(client)
    except Exception as exc:  # noqa: BLE001 - any failure to read the book blocks entries
        return blocked(f"cannot read the paper account: {exc}")
    try:
        queue = store.read()["trade"]
    except store.StoreError as exc:
        return blocked(str(exc))
    decision, new_state = guard.filter_entries(tickets, account, positions, orders, risk, state, queue,
                                               set(coids.values()), now)
    if save_state and new_state is not state:
        try:
            risk_state.save(new_state)
        except (risk_state.RiskStateError, OSError) as exc:
            print(f"warning: the peak equity was not saved: {exc}", file=sys.stderr)
    return decision


def new_entries(client: Any, tickets: list[dict], coids: dict[str, str]) -> list[dict]:
    """The tickets whose entry is not on the account yet (a lookup that fails counts as new; send_tickets
    reports it and sends nothing)."""
    out = []
    for t in tickets:
        try:
            if _existing(client, coids[t["symbol"]]) is None:
                out.append(t)
        except APIError:
            out.append(t)
    return out


def update_queue(queued: list[QueuedTicket], entries: list[Outcome], exits: list[Outcome],
                 decision: guard.Decision | None) -> list[str]:
    """Record what this run did on the Tradebot items, under the queue lock. Returns notes to print."""
    by_entry = {o.client_order_id: o for o in entries}
    by_exit = {o.client_order_id: o for o in exits}
    notes = []
    today = datetime.now(UTC).date().isoformat()
    with store.transaction() as queues:
        for q in queued:
            try:
                item = store.find(queues, q.item_id)
            except store.StoreError:
                continue
            t, symbol = q.ticket, q.ticket["symbol"]
            if decision is not None and item.get("status") == "ticketed":
                why = decision.reason_for(symbol)
                item["last_guard"] = {"at": decision.checked_at,
                                      "decision": "blocked" if decision.blocked else "skipped" if why else "allowed",
                                      "reason": why}
            e = by_entry.get(t["client_order_id"])
            if e is not None and item.get("status") == "ticketed":
                if e.action in ("sent", "already_sent"):
                    store.set_status(item, "sent", f"entry {e.client_order_id} on the paper account")
                elif e.action == "rejected":
                    store.set_status(item, "blocked", f"entry rejected: {e.error or _describe(e.order or {})}")
            x = by_exit.get(t["exit_order_id"])
            if x is None:
                continue
            if x.action in ("exit_sent", "exit_already_sent"):
                item["ticket"] = {**(item.get("ticket") or {}), "exit_done": True}
            elif x.action == "no_fill" and item.get("status") == "sent":
                past = item.get("ticket")
                item["past_tickets"] = [*(item.get("past_tickets") or []), past]
                item["ticket"] = None
                if q.expires_after and today > q.expires_after:
                    store.set_status(item, "expired", f"entry ended unfilled ({x.error}); past {q.expires_after}")
                else:
                    store.set_status(item, "armed", f"entry ended unfilled ({x.error}); armed again")
                notes.append(f"{symbol}: {x.error}; the item is {item['status']} again")
    return notes


# --- printing ----------------------------------------------------------------

def _describe(order: dict) -> str:
    filled = order.get("filled_qty") or "0"
    text = f"status {order.get('status')}, filled {filled} of {order.get('qty')}"
    if order.get("filled_avg_price"):
        text += f" @ {order['filled_avg_price']}"
    legs = order.get("legs") or []
    if legs:
        text += "; legs " + ", ".join(f"{leg.get('type')} {leg.get('status')}" for leg in legs)
    return text


def _ticket_line(as_of: date, t: dict, id_prefix: str | None = None) -> str:
    sign = "-" if t["side"] == LONG else "+"
    return (f"  {t['symbol']:<6}{t['side']:<6}{t['order_side']:<5}qty {t['quantity']:>6}  "
            f"limit {t['entry']:>9.2f}  then take-profit {t['target']:>9.2f}  "
            f"stop fill {sign} {t['stop_offset']:.2f} ({t['stop']:.2f} at entry)  id {client_order_id(as_of, t, id_prefix)}")


def _print_entries(outcomes: list[Outcome]) -> None:
    for o in outcomes:
        if o.action == "sent":
            print(f"  {o.symbol:<6}sent      {o.client_order_id}: {_describe(o.order)}")
        elif o.action == "already_sent":
            print(f"  {o.symbol:<6}not sent  {o.client_order_id} is already on the account: {_describe(o.order)}")
        elif o.action == "rejected":
            why = o.error or _describe(o.order)
            print(f"  {o.symbol:<6}REJECTED  {o.client_order_id}: {why}")
        else:
            print(f"  {o.symbol:<6}not sent  could not check {o.client_order_id}: {o.error}")


def _print_exits(outcomes: list[Outcome]) -> None:
    for o in outcomes:
        filled = f"filled {o.fill.qty} @ {o.fill.price:.2f}" if o.fill else ""
        if o.action == "exit_sent":
            print(f"  {o.symbol:<6}exit sent {o.client_order_id}: {filled}, stop {o.stop:.2f}; {_describe(o.order)}")
        elif o.action == "exit_already_sent":
            print(f"  {o.symbol:<6}no exit   {o.client_order_id} is already on the account: {_describe(o.order)}")
        elif o.action == "exit_rejected":
            print(f"  {o.symbol:<6}EXIT REJECTED {o.client_order_id}: {o.error or _describe(o.order)}")
        elif o.action == "exit_refused":
            print(f"  {o.symbol:<6}EXIT REFUSED  {o.client_order_id}: {o.error}")
        elif o.action == "exit_lookup_failed":
            print(f"  {o.symbol:<6}no exit   could not check {o.client_order_id}: {o.error}")
        elif o.action == "exit_waiting":
            print(f"  {o.symbol:<6}no exit   {o.error}; run --send again once it is done")
        else:
            print(f"  {o.symbol:<6}no exit   {o.error}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send one date's paper tickets to Alpaca paper: limit DAY entries, then OCO exits from the fills.")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--as-of", help="the ticket date, YYYY-MM-DD")
    which.add_argument("--manual", metavar="NAME", help="one manual ticket file name from <ticket dir>/manual/")
    which.add_argument("--tradebot", action="store_true", help="the Tradebot queue's ticketed items")
    parser.add_argument("--item", action="append", help="with --tradebot: only this queue item id; repeatable")
    parser.add_argument("--preview", action="store_true",
                        help="read the account (GET only) and show the risk guard's decisions; send nothing")
    parser.add_argument("--ticket-dir", help="folder of ticket files (default: ~/.tradingagents/tickets)")
    parser.add_argument("--send", action="store_true", help="send to Alpaca paper; without it nothing is sent")
    parser.add_argument("--wait", type=float, default=5.0, help="seconds to watch each new order for a fill")
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None,
         make_client: Callable[[Mapping[str, str]], Any] = paper_client, now: datetime | None = None,
         on_outcomes: Callable[[str, list[Outcome], list[Outcome], guard.Decision | None], None] | None = None) -> int:
    """on_outcomes(mode, entries, exits, decision) is called after a --send (the order log hooks in here)."""
    args = _parser().parse_args(argv)
    if args.send and args.preview:
        print("error: --preview sends nothing; use it without --send", file=sys.stderr)
        return EXIT_BAD_INPUT
    id_prefix = None
    queued: list[QueuedTicket] = []
    if args.as_of is not None:
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
            return EXIT_BAD_INPUT
    try:
        if args.tradebot:
            queued = load_tradebot(set(args.item or []) or None)
            tickets = [q.ticket for q in queued]
            path = None
        elif args.manual:
            path, data = load_manual(args.manual, args.ticket_dir)
            as_of, id_prefix = date.fromisoformat(data["as_of"]), manual_prefix(data)
            tickets = data["tickets"]
        else:
            path, data = load_latest_tickets(as_of, args.ticket_dir)
            tickets = data["tickets"]
    except TicketFileError as exc:
        print(f"error: {exc}; nothing sent", file=sys.stderr)
        return EXIT_BAD_INPUT

    # (as_of, prefix) per symbol: a Tradebot run mixes items, each with its own date and prefix
    where = {q.ticket["symbol"]: (q.as_of, q.prefix) for q in queued} if args.tradebot else \
        {t["symbol"]: (as_of, id_prefix) for t in tickets}
    to_enter = [q.ticket for q in queued if q.status == "ticketed"] if args.tradebot else tickets
    coids = {t["symbol"]: client_order_id(where[t["symbol"]][0], t, where[t["symbol"]][1]) for t in tickets}

    if args.tradebot:
        print(f"tradebot queue: {len(to_enter)} ticketed entr{'y' if len(to_enter) == 1 else 'ies'}, "
              f"{len(queued) - len(to_enter)} sent item(s) waiting for exits; each exit is placed from the actual fill")
        if not queued:
            print("nothing to send")
            return EXIT_OK
    else:
        what = "manual paper ticket" if args.manual else "paper tickets"
        print(f"{what} {path.name}: {len(tickets)} limit DAY entr{'y' if len(tickets) == 1 else 'ies'} "
              f"for {as_of}; each exit is placed from the actual fill")
    for t in tickets:
        print(_ticket_line(where[t["symbol"]][0], t, where[t["symbol"]][1]))
    if not args.send and not args.preview:
        print("dry run: nothing sent (add --preview to see the risk guard's decisions, --send to send to Alpaca "
              "paper)")
        return EXIT_OK

    if args.send:
        # The operator's trading window (Settings page): a --send outside it sends nothing.
        try:
            window = load_window()
        except SettingsError as exc:
            print(f"error: {exc}; nothing sent", file=sys.stderr)
            return EXIT_BAD_INPUT
        closed = window.closed_reason(now or datetime.now(UTC)) if window is not None else None
        if closed:
            print(f"error: outside the trading window: {closed} ({window.describe()}); nothing sent", file=sys.stderr)
            return EXIT_BAD_INPUT

    if env is None:
        load_dotenv(REPO_ROOT / ".env")
        env = os.environ
    try:
        client = make_client(env)
    except NotPaperKey as exc:
        print(f"error: {exc}; nothing sent", file=sys.stderr)
        return EXIT_BAD_INPUT

    fresh = new_entries(client, to_enter, coids)
    decision = guard_entries(client, fresh, coids, now, save_state=args.send)
    for line in guard.describe(decision):
        print(line)
    if args.preview:
        print("preview: nothing sent")
        return EXIT_GUARD_BLOCKED if decision.blocked else EXIT_OK

    print(f"sending to Alpaca paper ({PAPER_URL})")
    allowed = {t["symbol"] for t in decision.allowed}
    send = [t for t in to_enter if t["symbol"] in allowed or t not in fresh]  # on the account already: reported

    def per_ticket(fn, items, **kw) -> list[Outcome]:
        out = []
        for t in items:
            d, prefix = where[t["symbol"]]
            out += fn(client, d, [t], id_prefix=prefix, **kw)
        return out

    early = per_ticket(place_exits, [q.ticket for q in queued if q.status == "sent"]) if args.tradebot else []
    entries = per_ticket(send_tickets, send, wait=args.wait)
    print("entries:")
    _print_entries(entries)
    for t in to_enter:
        if t in fresh and t["symbol"] not in allowed:
            print(f"  {t['symbol']:<6}not sent  held back by the risk guard: {decision.reason_for(t['symbol'])}")
    exits = early + per_ticket(place_exits, to_enter)
    print("exits:")
    _print_exits(exits)
    if args.tradebot:
        try:
            for note in update_queue(queued, entries, exits, decision):
                print(f"  {note}")
        except store.StoreError as exc:
            print(f"warning: the Tradebot queue was not updated: {exc}", file=sys.stderr)
    if on_outcomes is not None:
        on_outcomes("tradebot" if args.tradebot else "manual" if args.manual else "daily", entries, exits, decision)
    problems = [o for o in entries + exits if o.action in PROBLEMS]
    if problems:
        return EXIT_ORDER_PROBLEM
    return EXIT_GUARD_BLOCKED if decision.skipped and not decision.allowed and fresh else EXIT_OK


if __name__ == "__main__":
    sys.exit(main(on_outcomes=ledger.record_send))  # every --send is logged
