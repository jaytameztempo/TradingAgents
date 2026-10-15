"""Paper-order tickets for the SIDE names still allowed after the earnings blackout. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_paper_tickets --as-of 2026-09-30

It refuses any import of Alpaca's trading client, then reads the newest daily
status, the newest earnings blackout, the ranked side handoff for the date and
the newest long and short channel reports for the date by the created_at inside
each file, not the ones the handoff names (from ~/.tradingagents/scanbot/ unless
--scan-dir is set). The newest report must carry the pivot_confirmed stage, or
there are no tickets; a name that is not a passing member of it is skipped. On a SIDE day with allowed
names it reads equity and buying power from the Alpaca paper account (one
read-only GET /v2/account through paper_orders.snapshot, paper keys only), and
writes one JSON of tickets to ~/.tradingagents/tickets/ unless --out-dir is
set. Entry is a limit at the channel level the scan measured: support from
the long channel report (limit buy), resistance from the short channel report
(limit sell), never the last close. The stop swing is, of the confirmed swings
in the 120 sessions ending on as_of, the highest swing low below support with
another swing low within 1 ATR(14) of it (long), or the lowest swing high above
resistance with another swing high within 1 ATR(14) (short); a swing more than
3 ATR(14) beyond support or resistance does not count. The stop is that
low - 1 ATR(14) snapped down to .99 or .49 (long), that high + 1 ATR(14)
snapped up to .01 or .51 (short), as the channel report recorded them; a name
with no such swing is skipped. The target is the midline.

The basket gets the lesser of 23% of the series' original buying power and the
buying power read now. The original buying power is the one read on the
series' first run, kept in ~/.tradingagents/tickets/series.json unless
--series-file is set; when the file is missing, or --new-series is given, the
buying power read now is recorded there and used. If the allocation is under 5%
of equity, no tickets are written. Otherwise each of the N allowed names with
usable levels gets allocation / N and floor(that / limit entry) shares. When
the basket's total stop-risk is over 8% of equity, every name's quantity is
scaled down by the same factor (rounded down) until it fits; a name left with
no share is skipped. Names in the
earnings blackout are listed as skipped with the earnings date. The TTM
Squeeze gate is not run and no market data is read. If the market label is
not SIDE, nothing is read from the account and nothing is written. No orders.
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
    parser.add_argument("--series-file", help="the series' original buying power "
                                              "(default: ~/.tradingagents/tickets/series.json)")
    parser.add_argument("--new-series", action="store_true",
                        help="start a new series: record the buying power read now as the original buying power")
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
        result = tickets_for_date(as_of, args.status_dir, args.handoff_dir, read_account, args.scan_dir,
                                  args.series_file, args.new_series)
    except TicketInputError as exc:
        print(f"error: {exc}; nothing written", file=sys.stderr)
        return EXIT_BAD_INPUT

    rules = result.rules
    print(f"paper tickets as of {result.as_of}  (market label {result.market_label})")
    if rules["account_equity"] is not None:
        print(f"  paper account: equity ${rules['account_equity']:,.2f}, buying power ${rules['buying_power']:,.2f} "
              f"(read {rules['account_read_at']})")
        recorded = " (recorded on this run)" if rules.get("series_recorded_this_run") else ""
        print(f"  series: original buying power ${rules['original_buying_power']:,.2f}{recorded}")
        print(f"  allocation ${rules['basket_allocation']:,.2f} (lesser of "
              f"{rules['basket_pct_of_original_buying_power']:.0%} of original buying power and buying power), "
              f"floor ${rules['allocation_floor']:,.2f} ({rules['floor_pct_of_equity']:.0%} of equity)")
        if rules["name_notional_limit"] is not None:
            print(f"  N = {rules['names_n']}: ${rules['name_notional_limit']:,.2f} a name (allocation / N)")
        if rules["book_risk_scale"] is not None:
            scaled = (f"over the limit, every quantity x {rules['book_risk_scale']:.4f}"
                      if rules["book_risk_scale"] < 1 else "within the limit")
            print(f"  book stop-risk ${rules['book_risk_before_scale']:,.2f} before scaling, limit "
                  f"${rules['book_risk_limit']:,.2f} ({rules['book_risk_pct_of_equity']:.0%} of equity): {scaled}")
    for t in result.tickets:
        sign = "-" if t.side == "long" else "+"
        print(f"  #{t.rank!s:<3}{t.symbol:<6}{t.side:<6}{t.order_side:<5}qty {t.quantity:>6}  limit {t.entry:>9.2f}  "
              f"stop {t.stop:>9.2f}  target {t.target:>9.2f}  risk ${t.risk_dollars:>9,.2f}  "
              f"notional ${t.notional:>10,.2f}  R:R {t.reward_risk:>5.2f}")
        swing = "low" if t.side == "long" else "high"
        print(f"       channel {t.line} {t.channel_level}; stop: clustered swing {swing} {t.stop_swing} "
              f"({t.stop_swing_date}) {sign} {rules['stop_atr_beyond_swing']:g} x ATR {t.atr_14:.4f} = {t.stop_calculated:.4f}, "
              f"snapped to {t.stop:.2f}")
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
