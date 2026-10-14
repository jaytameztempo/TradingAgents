"""Paper-order tickets for the SIDE names still allowed after the earnings blackout. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_paper_tickets --as-of 2026-09-30

It refuses any import of Alpaca's trading client, then reads the newest daily
status, the newest earnings blackout, the ranked side handoff for the date and
the long and short channel reports the handoff names (from
~/.tradingagents/scanbot/ unless --scan-dir is set). On a SIDE day with allowed
names it reads equity and buying power from the Alpaca paper account (one
read-only GET /v2/account through paper_orders.snapshot, paper keys only), and
writes one JSON of tickets to ~/.tradingagents/tickets/ unless --out-dir is
set. Entry is the channel line; the pivot the last confirmed daily ZigZag swing
beyond the entry (a low below a long entry, a high above a short one), and a
name with no such swing is skipped with the reason. The stop is pivot - 1 ATR
snapped down to .99 or .49 (long), pivot + 1 ATR snapped up to .01 or .51
(short); the target the midline. Each of the N allowed names with a usable
pivot gets buying power / N, at most 15% of buying power, and
floor(that / entry) shares; size does not depend on the stop. Names in the
earnings blackout are listed as skipped with the earnings date. If the market
label is not SIDE, nothing is read from the account and nothing is written. No
fetch of market data, no orders.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

from dotenv import load_dotenv

from extensions.status.paper_tickets import (
    Account,
    TicketInputError,
    TradingClientLoaded,
    account_from_alpaca,
    refuse_trading_client,
    save_tickets,
    tickets_for_date,
)
from paper_orders.snapshot import ACCOUNT_PATH, PAPER_URL, NotPaperKey, ReadFailed, paper_reader

EXIT_OK, EXIT_BAD_INPUT = 0, 2
REPO_ROOT = Path(__file__).resolve().parents[2]


def read_paper_account() -> Account:
    """Equity and buying power from the Alpaca paper account, now. Read-only."""
    load_dotenv(REPO_ROOT / ".env")
    try:
        raw = paper_reader(os.environ).account()
    except (NotPaperKey, ReadFailed) as exc:
        raise TicketInputError(f"cannot read the Alpaca paper account: {exc}") from exc
    return account_from_alpaca(raw, datetime.now(UTC), f"GET {PAPER_URL}{ACCOUNT_PATH}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paper-order tickets for the allowed SIDE names (no orders).")
    parser.add_argument("--as-of", required=True, help="the date to ticket, YYYY-MM-DD")
    parser.add_argument("--status-dir", help="daily status and earnings blackout (default: ~/.tradingagents/status)")
    parser.add_argument("--handoff-dir", help="ranked side handoffs (default: ~/.tradingagents/handoffs)")
    parser.add_argument("--scan-dir", help="long and short channel reports (default: ~/.tradingagents/scanbot)")
    parser.add_argument("--out-dir", help="folder for the tickets JSON (default: ~/.tradingagents/tickets)")
    return parser


def main(argv: list[str] | None = None, read_account: Callable[[], Account] = read_paper_account) -> int:
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
        result = tickets_for_date(as_of, args.status_dir, args.handoff_dir, read_account, args.scan_dir)
    except TicketInputError as exc:
        print(f"error: {exc}; nothing written", file=sys.stderr)
        return EXIT_BAD_INPUT

    rules = result.rules
    print(f"paper tickets as of {result.as_of}  (market label {result.market_label})")
    if rules["account_equity"] is not None:
        print(f"  paper account: equity ${rules['account_equity']:,.2f}, buying power ${rules['buying_power']:,.2f} "
              f"(read {rules['account_read_at']})")
        if rules["names_n"]:
            print(f"  N = {rules['names_n']}: ${rules['name_notional_limit']:,.2f} a name (buying power / N, at most "
                  f"{rules['name_notional_pct_of_buying_power']:.0%} of buying power)")
    for t in result.tickets:
        pivot, sign = t.pivot, "-" if t.side == "long" else "+"
        print(f"  #{t.rank!s:<3}{t.symbol:<6}{t.side:<6}{t.order_side:<5}qty {t.quantity:>6}  entry {t.entry:>9.2f}  "
              f"stop {t.stop:>9.2f}  target {t.target:>9.2f}  risk ${t.risk_dollars:>9,.2f}  "
              f"notional ${t.notional:>10,.2f}  R:R {t.reward_risk:>5.2f}")
        print(f"       pivot swing {'low' if t.side == 'long' else 'high'} {pivot['price']} of {pivot['date']} "
              f"(confirmed {pivot['confirmed_on']}) {sign} {rules['stop_atr_beyond_pivot']:g} x ATR {t.atr_14:.4f} "
              f"= {t.stop_calculated:.4f}, snapped to {t.stop:.2f}")
    if result.tickets:
        print(f"  book: notional ${result.total_notional:,.2f} of buying power ${rules['buying_power']:,.2f}, "
              f"stop-risk ${result.total_risk:,.2f}")
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
