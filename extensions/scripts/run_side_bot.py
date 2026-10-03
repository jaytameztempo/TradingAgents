"""SIDEBot: write a mean-reversion research plan for one routed symbol when the regime is SIDE.

It reads saved route files only. It imports no Alpaca module, fetches no bars, and places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_side_bot --as-of 2026-09-30 --symbol NVDA

Reads the latest route from ~/.tradingagents/routes/ (or --route-dir). If its regime is
SIDE and the symbol is in the route, writes the plan to ~/.tradingagents/plans/ unless
--out-dir is set. If the regime is UP or DOWN, it stops and writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from extensions.playbooks.side_bot import (
    MissingRoute,
    RegimeNotSide,
    SymbolNotInRoute,
    latest_route,
    make_plan,
    save_plan,
    validate_symbol,
)

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT, EXIT_WRONG_REGIME = 0, 1, 2, 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SIDEBot: write a research plan when the regime is SIDE (no orders).")
    parser.add_argument("--as-of", required=True, help="route date, YYYY-MM-DD")
    parser.add_argument("--symbol", required=True, help="one symbol from the route")
    parser.add_argument("--route-dir", help="folder of route JSON files (default: ~/.tradingagents/routes)")
    parser.add_argument("--out-dir", help="folder for the plan (default: ~/.tradingagents/plans)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        as_of = date.fromisoformat(args.as_of)
        symbol = validate_symbol(args.symbol)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        route_path, route = latest_route(as_of, args.route_dir)
        plan = make_plan(route, route_path, symbol)
    except RegimeNotSide as exc:
        print(f"SIDEBot blocked: {exc}; no plan written")
        print(f"  route file: {route_path}")
        return EXIT_WRONG_REGIME
    except (MissingRoute, SymbolNotInRoute) as exc:
        print(f"error: {exc}; no plan written", file=sys.stderr)
        return EXIT_FAILED

    path = save_plan(plan, args.out_dir)
    print(f"SIDEBot plan for {plan.symbol} as of {plan.as_of}: regime {plan.regime_label} (no order placed)")
    print(f"  route file: {route_path}")
    print(f"wrote {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
