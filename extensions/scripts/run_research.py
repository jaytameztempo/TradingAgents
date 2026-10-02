"""Research-only TradingAgents run on one routed ticker. It never places an order or calls Alpaca.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_research --as-of 2026-09-30 --ticker NVDA
    python -m extensions.scripts.run_research --as-of 2026-09-30 --ticker NVDA --analysts market news

Reads the latest route from ~/.tradingagents/routes/ (or --route-dir), prints ALLOWED
or BLOCKED, then runs TradingAgents and saves its report under TradingAgents' results folder.
The LLM provider and models come from TradingAgents' own settings in .env.
"""

from __future__ import annotations

import argparse
import sys

from extensions.market_data.alpaca_bars import validate_date_range, validate_ticker
from extensions.research import research_runner
from extensions.research.research_runner import (
    ANALYSTS,
    DEFAULT_ANALYSTS,
    TickerNotInRoute,
    latest_route,
    route_status,
    validate_analysts,
)
from extensions.router.strategy_router import MissingInput

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research-only TradingAgents run on a routed ticker (no orders).")
    parser.add_argument("--as-of", required=True, help="route date and analysis date, YYYY-MM-DD")
    parser.add_argument("--ticker", required=True, help="one ticker from the route")
    parser.add_argument("--analysts", nargs="+", default=list(DEFAULT_ANALYSTS), choices=ANALYSTS,
                        help=f"analysts to run (default: {' '.join(DEFAULT_ANALYSTS)})")
    parser.add_argument("--route-dir", help="folder of route JSON files (default: ~/.tradingagents/routes)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _, as_of = validate_date_range(args.as_of, args.as_of)
        ticker = validate_ticker(args.ticker)
        analysts = validate_analysts(args.analysts)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        route_path, decision = latest_route(as_of, args.route_dir)
        status = route_status(decision, ticker)
    except (MissingInput, TickerNotInRoute) as exc:
        print(f"error: {exc}; nothing was run", file=sys.stderr)
        return EXIT_FAILED

    print(f"{status}: {ticker} as of {as_of}")
    print(f"  route: {decision.reason}")
    print(f"  route file: {route_path}")
    print(f"Running TradingAgents (research only, analysts: {', '.join(analysts)}). No order will be placed.")

    try:
        result = research_runner.run_research(decision, ticker, analysts)
    except Exception as exc:  # an LLM or data-vendor failure ends the run; nothing else happens
        print(f"error: TradingAgents run failed: {exc}", file=sys.stderr)
        return EXIT_FAILED

    print(f"TradingAgents rating for {ticker}: {result.rating} (research only; no order placed)")
    print(f"{status}: {ticker}")
    print(f"report saved to {result.report_path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
