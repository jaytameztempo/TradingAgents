"""The kill switch: stop new entries, cancel every open order and close every position on the Alpaca paper account.

Run from the TradingAgents folder:

    python -m paper_orders.flatten                 # list what would be cancelled and closed; change nothing
    python -m paper_orders.flatten --send          # turn the kill switch on, then cancel and close everything

--send first turns the kill switch on in settings.json (the "risk" section the Settings page edits), so no
sender run, scheduled or not, can send an entry while or after the account is flattened; it stays on until the
operator turns it off. Then it asks the paper account to cancel all open orders (``cancel_orders``) and to close
all positions at market (``close_all_positions(cancel_orders=True)``), and prints what the account answered. The
kill switch and every answer are written to the order ledger (``paper_orders.ledger``).

Paper only, as the sender: the key ID must start with "PK" and the client is ``sender.paper_client`` on the
paper URL. A flatten is not limited to the trading window: closing is always allowed. Outside market hours the
account queues the closing orders for the next session.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from typing import Any

from dotenv import load_dotenv

from paper_orders import guard, ledger, sender
from paper_orders.trade_window import SettingsError

EXIT_OK, EXIT_BAD_INPUT, EXIT_PROBLEM = 0, 2, 3


def _rows(result: Any) -> list[dict]:
    """Alpaca answers a bulk cancel or close with one row per order or position (raw dicts)."""
    if result is None:
        return []
    return [dict(r) for r in (result if isinstance(result, list) else [result])]


def _status(row: dict) -> int | None:
    try:
        return int(row.get("status"))
    except (TypeError, ValueError):
        return None


def _print_book(positions: list[dict], orders: list[dict]) -> None:
    print(f"open positions: {len(positions)}")
    for p in positions:
        print(f"  {p.get('symbol') or '?':<6}{p.get('side') or '?':<6}qty {p.get('qty')}  "
              f"value {p.get('market_value')}")
    print(f"open orders: {len(orders)}")
    for o in orders:
        print(f"  {o.get('symbol') or '?':<6}{o.get('side') or '?':<5}{o.get('type') or '?':<8}"
              f"{o.get('order_class') or 'simple':<8}qty {o.get('qty')}  {o.get('status')}  "
              f"id {o.get('client_order_id') or o.get('id')}")


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None,
         make_client: Callable[[Mapping[str, str]], Any] = sender.paper_client,
         reason: str = "flatten from the command line") -> int:
    parser = argparse.ArgumentParser(description="Kill switch: cancel every open order and close every position "
                                                 "on the Alpaca paper account.")
    parser.add_argument("--send", action="store_true", help="do it; without it nothing changes")
    parser.add_argument("--reason", default=reason, help="recorded with the kill switch")
    args = parser.parse_args(argv)

    if env is None:
        load_dotenv(sender.REPO_ROOT / ".env")
        env = os.environ
    try:
        client = make_client(env)
    except sender.NotPaperKey as exc:
        print(f"error: {exc}; nothing changed", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        _, positions, orders = sender.read_book(client)
    except Exception as exc:  # noqa: BLE001 - still allow a flatten when the listing fails
        print(f"warning: cannot list the account first: {exc}", file=sys.stderr)
        positions, orders = [], []
    _print_book(positions, orders)
    if not args.send:
        print("dry run: nothing changed (add --send to turn the kill switch on and flatten)")
        return EXIT_OK

    try:
        section = guard.set_kill_switch(True, args.reason)
    except (SettingsError, OSError) as exc:
        print(f"error: cannot turn the kill switch on: {exc}; nothing changed", file=sys.stderr)
        return EXIT_BAD_INPUT
    print(f"kill switch ON at {section['kill_switch_at']} ({section['kill_switch_reason']}); no entry will be sent "
          "until it is turned off")
    print(f"flattening the Alpaca paper account ({sender.PAPER_URL})")
    rows = [ledger.line("flatten", "flatten", None, action="kill_switch_on", reason=section["kill_switch_reason"])]
    problems = 0
    try:
        cancelled = _rows(client.cancel_orders())
        bad = [r for r in cancelled if (_status(r) or 200) >= 400]
        problems += len(bad)
        rows += [ledger.line("flatten", "flatten", None, action="cancel",
                             reason=f"order {r.get('id')}: HTTP {r.get('status')}") for r in cancelled]
        print(f"cancel all open orders: {len(cancelled)} answered, {len(bad)} refused")
        for r in bad:
            print(f"  REFUSED {r.get('id')}: HTTP {r.get('status')} {r.get('body')}")
    except Exception as exc:  # noqa: BLE001 - go on to close the positions anyway
        problems += 1
        rows.append(ledger.line("flatten", "flatten", None, action="cancel_failed", reason=str(exc)))
        print(f"  cancel all open orders FAILED: {exc}")
    try:
        closed = _rows(client.close_all_positions(cancel_orders=True))
        bad = [r for r in closed if (_status(r) or 200) >= 400]
        problems += len(bad)
        rows += [ledger.line("flatten", "flatten", r.get("symbol"), action="close",
                             reason=f"HTTP {r.get('status')}") for r in closed]
        print(f"close all positions: {len(closed)} answered, {len(bad)} refused")
        for r in closed:
            word = "REFUSED" if r in bad else "closing"
            print(f"  {word} {r.get('symbol')}: HTTP {r.get('status')}")
    except Exception as exc:  # noqa: BLE001
        problems += 1
        rows.append(ledger.line("flatten", "flatten", None, action="close_failed", reason=str(exc)))
        print(f"  close all positions FAILED: {exc}")
    try:
        print(f"ledger: {ledger.append(rows)}")
    except ledger.LedgerError as exc:
        print(f"warning: the order ledger was not written: {exc}", file=sys.stderr)
    print("check the account with: python -m paper_orders.snapshot")
    return EXIT_PROBLEM if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
