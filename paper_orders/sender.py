"""Send one date's saved paper tickets to the Alpaca paper account, and nowhere else.

Run from the TradingAgents folder:

    python -m paper_orders.sender --as-of 2026-09-30           # print the orders, send nothing
    python -m paper_orders.sender --as-of 2026-09-30 --send    # send them to Alpaca paper

It loads the newest ``paper_tickets_<as_of>_*.json`` (by the created_at inside
each file) from ~/.tradingagents/tickets/ unless --ticket-dir is set, and sends
only the tickets in that file. Each ticket is one bracket limit DAY order: limit
at the ticket's entry, take-profit at its target, stop-loss at its stop, the
ticket's quantity. Nothing is resized or added.

Paper only:

- the key ID in ALPACA_API_KEY must start with "PK"; any other key is refused
  before a client is built,
- the client is always ``TradingClient(..., paper=True)`` with no URL override,
  and its base URL is checked against Alpaca's paper URL before any request.

Each order carries a client order ID, ``pt-<as_of>-<SYMBOL>-<buy|sell>``. Before
sending, the paper account is asked for that ID; if an order already has it,
its status is printed and nothing is sent, so a second run never double-sends.
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
from datetime import date, datetime
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
from extensions.status.paper_tickets import TICKET_VERSION, default_ticket_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
PAPER_URL = BaseURL.TRADING_PAPER.value
ORDER_SIDE = {LONG: "buy", SHORT: "sell"}
FINAL = {"filled", "rejected", "canceled", "expired", "done_for_day", "replaced"}
EXIT_OK, EXIT_BAD_INPUT, EXIT_ORDER_PROBLEM = 0, 2, 3


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
        entry, stop, target = t.get("entry"), t.get("stop"), t.get("target")
        if not all(isinstance(v, int | float) and not isinstance(v, bool) and v > 0 for v in (entry, stop, target)):
            raise bad(f"{symbol} has entry {entry!r}, stop {stop!r}, target {target!r}")
        sign = 1 if side == LONG else -1
        if not sign * (entry - stop) > 0 or not sign * (target - entry) > 0:
            raise bad(f"{symbol} {side}: stop {stop} and target {target} are not on either side of entry {entry}")


# --- orders ------------------------------------------------------------------

def client_order_id(as_of: date, ticket: dict) -> str:
    """The same ID on every run for one date, symbol and side."""
    return f"pt-{as_of}-{ticket['symbol']}-{ticket['order_side']}"


def order_request(as_of: date, ticket: dict) -> LimitOrderRequest:
    """One bracket limit DAY order from one ticket, prices and size as written."""
    return LimitOrderRequest(
        symbol=ticket["symbol"],
        qty=ticket["quantity"],
        side=OrderSide.BUY if ticket["order_side"] == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=ticket["entry"],
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=ticket["target"]),
        stop_loss=StopLossRequest(stop_price=ticket["stop"]),
        client_order_id=client_order_id(as_of, ticket),
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
    action: str  # sent, already_sent, rejected, lookup_failed
    order: dict | None = None
    error: str | None = None


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
    """Send each ticket once. A ticket whose client order ID is already on the account is not sent again."""
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
            order = client.submit_order(order_request(as_of, t))
        except APIError as exc:
            outcomes.append(Outcome(t["symbol"], coid, "rejected", error=_error_text(exc)))
            continue
        order = _settle(client, coid, order, wait, sleep)
        action = "rejected" if order.get("status") == "rejected" else "sent"
        outcomes.append(Outcome(t["symbol"], coid, action, order=order))
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
    return (f"  {t['symbol']:<6}{t['side']:<6}{t['order_side']:<5}qty {t['quantity']:>6}  "
            f"limit {t['entry']:>9.2f}  take-profit {t['target']:>9.2f}  stop {t['stop']:>9.2f}  "
            f"id {client_order_id(as_of, t)}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send one date's paper tickets to Alpaca paper (bracket limit DAY).")
    parser.add_argument("--as-of", required=True, help="the ticket date, YYYY-MM-DD")
    parser.add_argument("--ticket-dir", help="folder of ticket files (default: ~/.tradingagents/tickets)")
    parser.add_argument("--send", action="store_true", help="send to Alpaca paper; without it nothing is sent")
    parser.add_argument("--wait", type=float, default=5.0, help="seconds to watch each new order for a fill")
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None,
         make_client: Callable[[Mapping[str, str]], Any] = paper_client) -> int:
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
    print(f"paper tickets {path.name}: {len(tickets)} bracket limit DAY order(s) for {as_of}")
    for t in tickets:
        print(_ticket_line(as_of, t))
    if not args.send:
        print("dry run: nothing sent (add --send to send to Alpaca paper)")
        return EXIT_OK

    if env is None:
        load_dotenv(REPO_ROOT / ".env")
        env = os.environ
    try:
        client = make_client(env)
    except NotPaperKey as exc:
        print(f"error: {exc}; nothing sent", file=sys.stderr)
        return EXIT_BAD_INPUT

    print(f"sending to Alpaca paper ({PAPER_URL})")
    outcomes = send_tickets(client, as_of, tickets, wait=args.wait)
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
    problems = [o for o in outcomes if o.action in ("rejected", "lookup_failed")]
    return EXIT_ORDER_PROBLEM if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
