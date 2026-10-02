"""Print or save daily bars from Alpaca, read-only. It cannot place orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.fetch_bars SPY --start 2026-06-01 --end 2026-09-30
    python -m extensions.scripts.fetch_bars SPY QQQ IWM --start 2026-01-02 --end 2026-09-30 --out bars.csv

Keys are read from ALPACA_API_KEY and ALPACA_SECRET_KEY, in the environment or in .env.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import find_dotenv, load_dotenv

from extensions.market_data.alpaca_bars import (
    AlpacaDataError,
    MissingAlpacaKeys,
    fetch_daily_bars,
    validate_date_range,
    validate_ticker,
)

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch daily bars from Alpaca (read-only, no orders).")
    parser.add_argument("tickers", nargs="+", help="one or more US tickers, e.g. SPY QQQ")
    parser.add_argument("--start", required=True, help="first date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="last date, YYYY-MM-DD (not after today)")
    parser.add_argument("--feed", default="iex", choices=["iex", "sip", "delayed_sip"],
                        help="data feed; free accounts can query iex (default)")
    parser.add_argument("--adjustment", default="split", choices=["raw", "split", "dividend", "all"],
                        help="price adjustment (default: split)")
    parser.add_argument("--out", help="write all bars to this CSV file")
    parser.add_argument("--cache-dir", help="reuse bars for completed ranges from this folder")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        tickers = [validate_ticker(t) for t in args.tickers]
        start, end = validate_date_range(args.start, args.end)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    load_dotenv(find_dotenv(usecwd=True))
    try:
        bars = fetch_daily_bars(
            tickers, start, end, feed=args.feed, adjustment=args.adjustment, cache_dir=args.cache_dir
        )
    except (MissingAlpacaKeys, AlpacaDataError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED

    for ticker in tickers:
        rows = bars[bars["symbol"] == ticker]
        if rows.empty:
            print(f"{ticker}: no bars from {start} to {end}")
            continue
        last = rows.iloc[-1]
        print(
            f"{ticker}: {len(rows)} bars, {rows['date'].iloc[0]:%Y-%m-%d} to {last['date']:%Y-%m-%d}, "
            f"last close {last['close']:.2f}"
        )

    if args.out:
        bars.to_csv(args.out, index=False)
        print(f"wrote {len(bars)} rows to {args.out}")

    return EXIT_OK if not bars.empty else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
