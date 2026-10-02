"""Label one symbol UP, DOWN or SIDE and write the label as JSON. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_regime_bot --as-of 2026-09-30
    python -m extensions.scripts.run_regime_bot --as-of 2026-09-30 --symbol QQQ --out-dir regimes

The label goes to ~/.tradingagents/regimes/ unless --out-dir is set.
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
from extensions.regime.regime_bot import DEFAULT_SYMBOL, run_regime_bot, save_label

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Label a symbol UP, DOWN or SIDE (read-only, no orders).")
    parser.add_argument("--as-of", required=True, help="label date, YYYY-MM-DD (not after today)")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help=f"symbol to label (default: {DEFAULT_SYMBOL})")
    parser.add_argument("--out-dir", help="folder for the label JSON (default: ~/.tradingagents/regimes)")
    parser.add_argument("--cache-dir", help="reuse bars for completed ranges from this folder")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        symbol = validate_ticker(args.symbol)
        _, as_of = validate_date_range(args.as_of, args.as_of)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    load_dotenv(find_dotenv(usecwd=True))
    try:
        label = run_regime_bot(as_of, symbol, cache_dir=args.cache_dir)
    except (MissingAlpacaKeys, AlpacaDataError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED

    print(f"{label.symbol} as of {label.as_of}: {label.label}")
    for reason in label.reasons:
        print(f"  - {reason}")

    path = save_label(label, args.out_dir)
    print(f"wrote {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
