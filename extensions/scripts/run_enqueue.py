"""Queue one date's router-allowed candidates for the Tradebot. It never sends an order.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_enqueue --as-of 2026-10-09
    python -m extensions.scripts.run_enqueue --as-of 2026-10-09 --category 2 --dry-run

The market label on the date's SPY+QQQ route picks the source: the earnings blackout's allowed SIDE names, or the
UP or DOWN category basket for --category (default: the Settings page's automation category). See
extensions.tradebot.enqueue.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date

from extensions.status.paper_tickets import TradingClientLoaded, refuse_trading_client
from extensions.tradebot import config, store
from extensions.tradebot.enqueue import enqueue_for_date
from extensions.tradebot.sources import CATEGORIES, SourceError
from paper_orders.trade_window import SettingsError, load_settings

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Queue one date's router-allowed candidates for the Tradebot.")
    parser.add_argument("--as-of", required=True, help="the scan date, YYYY-MM-DD")
    parser.add_argument("--category", type=int, choices=CATEGORIES, help="UP/DOWN basket category")
    parser.add_argument("--dry-run", action="store_true", help="show what would be queued; queue nothing")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        refuse_trading_client()
        as_of = date.fromisoformat(args.as_of)
        settings = config.load()
        category = args.category or (load_settings().get("automation") or {}).get("category") or 1
    except TradingClientLoaded as exc:
        print(f"error: {exc}; nothing queued", file=sys.stderr)
        return EXIT_BAD_INPUT
    except ValueError as exc:  # a bad date, or SettingsError
        print(f"error: {exc}; nothing queued", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        result = enqueue_for_date(as_of, int(category), settings["max_enqueue"], settings["expire_sessions"],
                                  dry_run=args.dry_run)
    except (SourceError, store.StoreError, SettingsError) as exc:
        print(f"error: {exc}; nothing queued", file=sys.stderr)
        return EXIT_FAILED
    if args.json:
        print(json.dumps({**asdict(result), "as_of": result.as_of.isoformat()}, indent=2))
        return EXIT_OK
    print(f"tradebot enqueue {as_of}: market label {result.label} ({result.route_file})")
    print(f"  candidates      {', '.join(result.candidates) or 'none'}")
    if result.capped:
        print(f"  over the cap    {', '.join(result.capped)}")
    print(f"  added           {', '.join(result.added) or 'none'}")
    print(f"  already queued  {', '.join(result.already_queued) or 'none'}")
    if result.note:
        print(f"  {result.note}")
    print("  no order sent")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
