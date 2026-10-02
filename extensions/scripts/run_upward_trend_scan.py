"""Run SCAN-Upward Trend Momentum and write a TickerBasket JSON file. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_upward_trend_scan --as-of 2026-09-30
    python -m extensions.scripts.run_upward_trend_scan --as-of 2026-09-30 --tickers SPY QQQ --out-dir baskets

The basket goes to ~/.tradingagents/baskets/ unless --out-dir is set.
Keys are read from ALPACA_API_KEY and ALPACA_SECRET_KEY, in the environment or in .env.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import find_dotenv, load_dotenv

from extensions.market_data.alpaca_bars import (
    AlpacaDataError,
    MissingAlpacaKeys,
    validate_date_range,
    validate_ticker,
)
from extensions.scans.basket import save_basket
from extensions.scans.upward_trend_momentum import DEFAULT_UNIVERSE, SCAN_NAME, run_scan

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Run {SCAN_NAME} and write a TickerBasket (no orders).")
    parser.add_argument("--as-of", required=True, help="scan date, YYYY-MM-DD (not after today)")
    parser.add_argument("--tickers", nargs="+", default=list(DEFAULT_UNIVERSE),
                        help=f"tickers to scan (default: {' '.join(DEFAULT_UNIVERSE)})")
    parser.add_argument("--out-dir", help="folder for the basket JSON (default: ~/.tradingagents/baskets)")
    parser.add_argument("--cache-dir", help="reuse bars for completed ranges from this folder")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        tickers = [validate_ticker(t) for t in args.tickers]
        _, as_of = validate_date_range(args.as_of, args.as_of)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    load_dotenv(find_dotenv(usecwd=True))
    try:
        basket = run_scan(as_of, tickers, cache_dir=args.cache_dir)
    except (MissingAlpacaKeys, AlpacaDataError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED

    print(f"{SCAN_NAME} as of {as_of}: {len(basket.members)} of {len(basket.universe)} passed")
    for member in basket.members:
        print(f"  PASS {member.ticker:<6} score {member.score:+.2%}  close {member.metrics['close']:.2f}")
    for rejection in basket.rejected:
        print(f"  FAIL {rejection.ticker:<6} {rejection.reason}")

    path = save_basket(basket, args.out_dir)
    print(f"wrote {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
