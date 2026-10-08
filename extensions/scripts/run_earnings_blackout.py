"""Earnings blackout for the confirmed SIDE names in the daily status. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_earnings_blackout --as-of 2026-09-30

It reads the newest daily status for the date and each confirmed name's cached
SEC EDGAR company facts, and writes one JSON and one markdown status note to
~/.tradingagents/status/ unless --out-dir is set. Earnings within 3 sessions of
the date remove the name from the allowed list; within 7 sessions it is flagged
and kept; no date is flagged unknown and kept. Estimated dates are labelled
estimated. Without a daily status nothing is written. It reads saved files
only: no fetch, no Alpaca call, no orders.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from extensions.status.daily_status import SIDES
from extensions.status.earnings_blackout import BlackoutInputError, blackout_for_date, save_blackout

EXIT_OK, EXIT_BAD_INPUT = 0, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Earnings blackout from the daily status and cached SEC facts (no orders).")
    parser.add_argument("--as-of", required=True, help="the date to check, YYYY-MM-DD")
    parser.add_argument("--status-dir", help="where to look for daily status (default: ~/.tradingagents/status)")
    parser.add_argument("--fundamentals-dir", help="cached SEC facts (default: ~/.tradingagents/fundamentals)")
    parser.add_argument("--out-dir", help="folder for the JSON and note (default: ~/.tradingagents/status)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        blackout = blackout_for_date(as_of, args.status_dir, args.fundamentals_dir)
    except BlackoutInputError as exc:
        print(f"error: {exc}; nothing written", file=sys.stderr)
        return EXIT_BAD_INPUT

    print(f"earnings blackout as of {blackout.as_of}  (market label {blackout.market_label})")
    for c in blackout.names:
        away = "-" if c.sessions_away is None else c.sessions_away
        print(f"  {c.symbol:<6}{c.side:<6}{c.earnings_date or '-':<11} {c.date_source or '-':<10} "
              f"sessions {away!s:<4}{c.result}")
    for side in SIDES:
        print(f"  allowed {side + ':':<6} {' '.join(blackout.allowed[side]) or '-'}")
    print(f"  removed: {', '.join(blackout.removed) or 'none'}")
    print(f"  flagged and kept: {', '.join(blackout.flagged_kept) or 'none'}")
    print(f"  unknown and kept: {', '.join(blackout.unknown_kept) or 'none'}")
    print(f"  source: {blackout.source}")
    print("  read only; no order placed")
    for warning in blackout.warnings:
        print(f"warning: {warning}")

    json_path, note_path = save_blackout(blackout, args.out_dir)
    print(f"wrote {json_path}")
    print(f"wrote {note_path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
