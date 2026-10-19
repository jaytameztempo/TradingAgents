"""Send one date's saved paper tickets to the Alpaca paper account, and nowhere else.

Run from the TradingAgents folder:

    python -m paper_orders.sender --as-of 2026-09-30           # print the orders, send nothing
    python -m paper_orders.sender --as-of 2026-09-30 --send    # send them to Alpaca paper

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
each exit ``pt-<as_of>-<SYMBOL>-exit``. Before sending either, the paper
account is asked for that ID; if an order already has it, its status is printed
and nothing is sent, so a second run never double-sends.
A --send outside the operator's trading window (``paper_orders.trade_window``, set on the UI's Settings
page) sends nothing; with no window saved, nothing is gated. A dry run is allowed at any time.
The ticket files, scans, router and bots are not touched.
"""

from __future__ import annotations

import argparse
import json
import os
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
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest
from dotenv import load_dotenv

from extensions.market_data.alpaca_bars import API_KEY_ENV, PAPER_KEY_PREFIX, SECRET_KEY_ENV, validate_ticker
from extensions.status.daily_status import LONG, SHORT, SIDE
from extensions.status.paper_tickets import TICKET_VERSION, default_ticket_dir, stop_for_fill
from paper_orders.trade_window import SettingsError, load_window

REPO_ROOT = Path(__file__).resolve().parents[1]
PAPER_URL = BaseURL.TRADING_PAPER.value
ORDER_SIDE = {LONG: "buy", SHORT: "sell"}
FINAL = {"filled", "rejected", "canceled", "expired", "done_for_day", "replaced"}
EXIT_OK, EXIT_BAD_INPUT, EXIT_ORDER_PROBLEM = 0, 2, 3
PROBLEMS = {"rejected", "lookup_failed", "exit_rejected", "exit_refused", "exit_lookup_failed"}


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
        symbol = t.get("symbol")
        try:
            if validate_ticker(symbol) != symbol:
                raise ValueError("not upper-case and trimmed")
        except ValueError as exc:
            raise bad(f"ticket symbol {symbol!r}: {exc}") from exc
        if symbol in seen:
            raise bad(f"{symbol} is ticketed twice")
        seen.add(symbol)
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


def _is_price(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and v > 0


# --- orders ------------------------------------------------------------------

def client_order_id(as_of: date, ticket: dict) -> str:
    """The entry's ID, the same on every run for one date, symbol and side."""
    return f"pt-{as_of}-{ticket['symbol']}-{ticket['order_side']}"


def exit_order_id(as_of: date, ticket: dict) -> str:
    """The exit pair's ID, the same on every run for one date and symbol."""
    return f"pt-{as_of}-{ticket['symbol']}-exit"


def entry_request(as_of: date, ticket: dict) -> LimitOrderRequest:
    """One plain limit DAY order from one ticket, entry and size as written, nothing attached."""
    return LimitOrderRequest(
        symbol=ticket["symbol"],
        qty=ticket["quantity"],
        side=OrderSide.BUY if ticket["order_side"] == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=ticket["entry"],
        order_class=OrderClass.SIMPLE,
        client_order_id=client_order_id(as_of, ticket),
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


def exit_request(as_of: date, ticket: dict, fill: Fill) -> LimitOrderRequest:
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
        client_order_id=exit_order_id(as_of, ticket),
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
                 sleep: Callable[[float], None] = time.sleep) -> list[Outcome]:
    """Send each ticket's entry once. An entry whose client order ID is already on the account is not sent again."""
    outcomes = []
    for t in tickets:
        coid = client_order_id(as_of, t)
        try:
            found = _existing(client, coid)
        except APIError as exc:
            outcomes.append(Outcome(t["symbol"], coid, "lookup_failed", error=_error_text(exc)))
            continue
        if found is not None:
            outcomes.append(Outcome(t["symbol"], coid, "already_sent", order=found))
            continue
        try:
            order = client.submit_order(entry_request(as_of, t))
        except APIError as exc:
            outcomes.append(Outcome(t["symbol"], coid, "rejected", error=_error_text(exc)))
            continue
        order = _settle(client, coid, order, wait, sleep)
        action = "rejected" if order.get("status") == "rejected" else "sent"
        outcomes.append(Outcome(t["symbol"], coid, action, order=order))
    return outcomes


def place_exits(client: Any, as_of: date, tickets: list[dict]) -> list[Outcome]:
    """For each entry done filling, send its OCO exit once, the stop measured from the actual fill."""
    outcomes = []
    for t in tickets:
        entry_id, coid = client_order_id(as_of, t), exit_order_id(as_of, t)
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
            req = exit_request(as_of, t, fill)
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


def _ticket_line(as_of: date, t: dict) -> str:
    sign = "-" if t["side"] == LONG else "+"
    return (f"  {t['symbol']:<6}{t['side']:<6}{t['order_side']:<5}qty {t['quantity']:>6}  "
            f"limit {t['entry']:>9.2f}  then take-profit {t['target']:>9.2f}  "
            f"stop fill {sign} {t['stop_offset']:.2f} ({t['stop']:.2f} at entry)  id {client_order_id(as_of, t)}")


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
    parser.add_argument("--as-of", required=True, help="the ticket date, YYYY-MM-DD")
    parser.add_argument("--ticket-dir", help="folder of ticket files (default: ~/.tradingagents/tickets)")
    parser.add_argument("--send", action="store_true", help="send to Alpaca paper; without it nothing is sent")
    parser.add_argument("--wait", type=float, default=5.0, help="seconds to watch each new order for a fill")
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None,
         make_client: Callable[[Mapping[str, str]], Any] = paper_client, now: datetime | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        path, data = load_latest_tickets(as_of, args.ticket_dir)
    except TicketFileError as exc:
        print(f"error: {exc}; nothing sent", file=sys.stderr)
        return EXIT_BAD_INPUT

    tickets = data["tickets"]
    print(f"paper tickets {path.name}: {len(tickets)} limit DAY entr{'y' if len(tickets) == 1 else 'ies'} "
          f"for {as_of}; each exit is placed from the actual fill")
    for t in tickets:
        print(_ticket_line(as_of, t))
    if not args.send:
        print("dry run: nothing sent (add --send to send to Alpaca paper)")
        return EXIT_OK

    # The operator's trading window (Settings page): a --send outside it sends nothing.
    try:
        window = load_window()
    except SettingsError as exc:
        print(f"error: {exc}; nothing sent", file=sys.stderr)
        return EXIT_BAD_INPUT
    if window is not None and not window.allows(now or datetime.now(UTC)):
        print(f"error: outside the trading window ({window.describe()}); nothing sent", file=sys.stderr)
        return EXIT_BAD_INPUT

    if env is None:
        load_dotenv(REPO_ROOT / ".env")
        env = os.environ
    try:
        client = make_client(env)
    except NotPaperKey as exc:
        print(f"error: {exc}; nothing sent", file=sys.stderr)
        return EXIT_BAD_INPUT

    print(f"sending to Alpaca paper ({PAPER_URL})")
    entries = send_tickets(client, as_of, tickets, wait=args.wait)
    print("entries:")
    _print_entries(entries)
    exits = place_exits(client, as_of, tickets)
    print("exits:")
    _print_exits(exits)
    problems = [o for o in entries + exits if o.action in PROBLEMS]
    return EXIT_ORDER_PROBLEM if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
