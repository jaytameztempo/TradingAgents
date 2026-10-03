"""The command line shared by UPBot and DOWNBot. See run_up_bot.py and run_down_bot.py.

It reads saved route files only. It imports no Alpaca module, fetches no bars, and places no orders.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from extensions.playbooks.trend_bots import (
    MissingRoute,
    RegimeMismatch,
    SymbolNotInRoute,
    TrendBot,
    latest_route,
    make_plan,
    save_plan,
    validate_symbol,
)

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT, EXIT_WRONG_REGIME = 0, 1, 2, 3


def _parser(bot: TrendBot) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"{bot.name}: write a research plan when the regime is {bot.regime} (no orders)."
    )
    parser.add_argument("--as-of", required=True, help="route date, YYYY-MM-DD")
    parser.add_argument("--symbol", required=True, help="one symbol from the route")
    parser.add_argument("--route-dir", help="folder of route JSON files (default: ~/.tradingagents/routes)")
    parser.add_argument("--out-dir", help="folder for the plan (default: ~/.tradingagents/plans)")
    return parser


def main(bot: TrendBot, argv: list[str] | None = None) -> int:
    args = _parser(bot).parse_args(argv)
    try:
        as_of = date.fromisoformat(args.as_of)
        symbol = validate_symbol(args.symbol)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        route_path, route = latest_route(as_of, args.route_dir)
        plan = make_plan(bot, route, route_path, symbol)
    except RegimeMismatch as exc:
        print(f"{bot.name} blocked: {exc}; no plan written")
        print(f"  route file: {route_path}")
        return EXIT_WRONG_REGIME
    except (MissingRoute, SymbolNotInRoute) as exc:
        print(f"error: {exc}; no plan written", file=sys.stderr)
        return EXIT_FAILED

    path = save_plan(plan, args.out_dir)
    print(f"{bot.name} plan for {plan.symbol} as of {plan.as_of}: regime {plan.regime_label} (no order placed)")
    print(f"  route file: {route_path}")
    print(f"wrote {path}")
    return EXIT_OK
