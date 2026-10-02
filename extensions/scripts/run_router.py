"""Decide whether UP playbooks may run on the upward-trend basket, from the SPY regime label.

It reads saved files only. It fetches no bars and places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_router --as-of 2026-09-30

Reads ~/.tradingagents/baskets/ and ~/.tradingagents/regimes/ (or --basket-dir and
--regime-dir) and writes the decision to ~/.tradingagents/routes/ unless --out-dir is set.
"""

from __future__ import annotations

import argparse
import sys

from extensions.market_data.alpaca_bars import validate_date_range
from extensions.router.strategy_router import MissingInput, route_for_date, save_decision

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Route the upward-trend basket by the SPY regime (read-only).")
    parser.add_argument("--as-of", required=True, help="date of the basket and regime label, YYYY-MM-DD")
    parser.add_argument("--basket-dir", help="folder of basket JSON files (default: ~/.tradingagents/baskets)")
    parser.add_argument("--regime-dir", help="folder of regime JSON files (default: ~/.tradingagents/regimes)")
    parser.add_argument("--out-dir", help="folder for the decision JSON (default: ~/.tradingagents/routes)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _, as_of = validate_date_range(args.as_of, args.as_of)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        decision = route_for_date(as_of, args.basket_dir, args.regime_dir)
    except (MissingInput, ValueError) as exc:
        print(f"error: {exc}; no decision written", file=sys.stderr)
        return EXIT_FAILED

    print(f"{decision.regime_symbol} regime as of {decision.as_of}: {decision.regime_label}")
    if decision.up_allowed:
        print("UP playbooks: ALLOWED")
        print(f"  tradeable under UP: {', '.join(decision.tradeable) or '(no passed tickers)'}")
    else:
        print("UP playbooks: BLOCKED")
        print(f"  not tradeable under UP: {', '.join(decision.blocked) or '(no passed tickers)'}")

    path = save_decision(decision, args.out_dir)
    print(f"wrote {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
