"""Decide which trend bot may run on which names, from the SPY regime label and both scan baskets.

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
    parser = argparse.ArgumentParser(
        description="Route the upward and breakdown baskets by the SPY regime (read-only)."
    )
    parser.add_argument("--as-of", required=True, help="date of the baskets and regime label, YYYY-MM-DD")
    parser.add_argument("--basket-dir", help="folder of basket JSON files (default: ~/.tradingagents/baskets)")
    parser.add_argument("--regime-dir", help="folder of regime JSON files (default: ~/.tradingagents/regimes)")
    parser.add_argument("--out-dir", help="folder for the decision JSON (default: ~/.tradingagents/routes)")
    return parser


def _names(passed: list[str], basket_file: str | None) -> str:
    if basket_file is None:
        return "no basket"
    return ", ".join(passed) or "none"


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
    print(f"UPBot: {'ALLOWED' if decision.up_allowed else 'BLOCKED'}"
          f"  (upward scan passed: {_names(decision.up_passed, decision.basket_file)})")
    print(f"DOWNBot: {'ALLOWED' if decision.down_allowed else 'BLOCKED'}"
          f"  (breakdown scan passed: {_names(decision.down_passed, decision.down_basket_file)})")
    if decision.regime_label == "SIDE":
        print(f"routed to SIDEBot only: {', '.join(decision.blocked) or '(no passed names)'}")

    path = save_decision(decision, args.out_dir)
    print(f"wrote {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
