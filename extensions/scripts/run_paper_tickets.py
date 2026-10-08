"""Paper-order tickets for the SIDE names still allowed after the earnings blackout. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_paper_tickets --as-of 2026-09-30

It refuses any import of Alpaca's trading client, then reads the newest daily
status, the newest earnings blackout and the ranked side handoff for the date,
and writes one JSON of tickets to ~/.tradingagents/tickets/ unless --out-dir is
set. Entry is the channel line, the stop 0.25 ATR beyond it, the target the
midline. Size is the smaller of 1% risk and 10% notional of a placeholder
$100,000 account. If the market label is not SIDE, nothing is written. It reads
saved files only: no fetch, no Alpaca call, no orders.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from extensions.status.paper_tickets import (
    ACCOUNT_EQUITY,
    TicketInputError,
    TradingClientLoaded,
    refuse_trading_client,
    save_tickets,
    tickets_for_date,
)

EXIT_OK, EXIT_BAD_INPUT = 0, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paper-order tickets for the allowed SIDE names (no orders).")
    parser.add_argument("--as-of", required=True, help="the date to ticket, YYYY-MM-DD")
    parser.add_argument("--status-dir", help="daily status and earnings blackout (default: ~/.tradingagents/status)")
    parser.add_argument("--handoff-dir", help="ranked side handoffs (default: ~/.tradingagents/handoffs)")
    parser.add_argument("--out-dir", help="folder for the tickets JSON (default: ~/.tradingagents/tickets)")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        refuse_trading_client()
    except TradingClientLoaded as exc:
        print(f"error: {exc}; nothing written", file=sys.stderr)
        return EXIT_BAD_INPUT
    args = _parser().parse_args(argv)
    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        result = tickets_for_date(as_of, args.status_dir, args.handoff_dir)
    except TicketInputError as exc:
        print(f"error: {exc}; nothing written", file=sys.stderr)
        return EXIT_BAD_INPUT

    print(f"paper tickets as of {result.as_of}  (market label {result.market_label}, "
          f"placeholder account ${ACCOUNT_EQUITY:,.0f})")
    for t in result.tickets:
        print(f"  {t.symbol:<6}{t.side:<6}{t.order_side:<5}qty {t.quantity:>6}  entry {t.entry:>9.2f}  "
              f"stop {t.stop:>9.2f}  target {t.target:>9.2f}  risk ${t.risk_dollars:>8,.2f}  "
              f"notional ${t.notional:>10,.2f}  R:R {t.reward_risk:>5.2f}  bound by {t.size_bound_by}")
    for symbol, why in result.skipped.items():
        print(f"  skipped {symbol}: {why}")
    print("  paper only; no order placed")
    for warning in result.warnings:
        print(f"warning: {warning}")

    if not result.tickets:
        print(f"{result.no_ticket_reason}; nothing written")
        return EXIT_OK
    path = save_tickets(result, args.out_dir)
    print(f"wrote {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
