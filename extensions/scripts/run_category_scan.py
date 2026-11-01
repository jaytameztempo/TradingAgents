"""Run SCANUP, SCANDOWN or SCANSIDE at a strictness category and write TickerBasket JSON. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_category_scan --scan up --as-of 2026-09-30
    python -m extensions.scripts.run_category_scan --scan down --as-of 2026-09-30 --category 2
    python -m extensions.scripts.run_category_scan --scan side --financials-report path/to/financials_....json --category 3

--category is 1 (strictest, the default), 2 (balanced) or 3 (loosest); the rules
for each are in extensions/scans/categories.py. --as-of picks the newest
financials report for that date in --report-dir. The candidates, financial
groups and borrow flags come from that report, the universe report behind it,
and that report's asset snapshot unless --assets-snapshot is set. Bars are
split-adjusted SIP daily bars dated on or before the report's as-of date.

SCANUP and SCANDOWN write one basket; SCANSIDE writes a long and a short basket.
They go to ~/.tradingagents/baskets/ unless --out-dir is set, and an existing
basket is never overwritten. If Alpaca refuses the bars, or SPY's bars do not
cover the RS window (SCANUP and SCANDOWN), nothing is written. Keys are read
from ALPACA_API_KEY and ALPACA_SECRET_KEY, in the environment or in .env.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from extensions.market_data.alpaca_bars import MissingAlpacaKeys
from extensions.scanbot.funnel import DATA_ERRORS
from extensions.scans import scan_down_category, scan_side_category, scan_up_category
from extensions.scans.basket import save_basket
from extensions.scans.categories import CATEGORIES, DEFAULT_CATEGORY, params_for
from extensions.scans.category_inputs import MissingBenchmark, load_inputs
from extensions.scripts.run_scanbot_trend import newest_financials_report

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2
READ_ERRORS = (OSError, ValueError, KeyError, TypeError, StopIteration)
SCANS = {"up": scan_up_category, "down": scan_down_category, "side": scan_side_category}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a category scan (SCANUP, SCANDOWN or SCANSIDE) and write TickerBasket JSON (no orders)."
    )
    parser.add_argument("--scan", required=True, choices=sorted(SCANS), help="which regime scan to run")
    parser.add_argument("--category", type=int, choices=CATEGORIES, default=DEFAULT_CATEGORY,
                        help=f"1 strictest, 2 balanced, 3 loosest (default: {DEFAULT_CATEGORY})")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--financials-report", help="the financials report JSON whose universe to scan")
    source.add_argument("--as-of", help="use the newest financials report for this date, YYYY-MM-DD")
    parser.add_argument("--report-dir", help="where to look for financials reports (default: ~/.tradingagents/scanbot)")
    parser.add_argument("--assets-snapshot",
                        help="asset snapshot for shortable / easy_to_borrow (default: the one behind the universe report)")
    parser.add_argument("--cache-dir", help="reuse bars for completed ranges from this folder")
    parser.add_argument("--out-dir", help="folder for the basket JSON (default: ~/.tradingagents/baskets)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.financials_report:
        path = Path(args.financials_report)
    else:
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
            return EXIT_BAD_INPUT
        path = newest_financials_report(as_of, args.report_dir)
        if path is None:
            print(f"error: no financials report for {as_of}; run run_scanbot_financials first", file=sys.stderr)
            return EXIT_BAD_INPUT
    try:
        inputs = load_inputs(path, args.assets_snapshot)
    except READ_ERRORS as exc:
        print(f"error: cannot read the inputs behind financials report {path}: {exc!r}", file=sys.stderr)
        return EXIT_BAD_INPUT

    params = params_for(args.scan, args.category)
    load_dotenv(find_dotenv(usecwd=True))
    print(f"financials report {path}: {len(inputs.universe)} universe survivors, {len(inputs.strong)} STRONG, "
          f"{len(inputs.weak)} WEAK, {len(inputs.neutral)} NEUTRAL as of {inputs.as_of}")
    print(f"shortable / easy_to_borrow from {inputs.sources['assets_snapshot']} "
          f"(fetched {inputs.sources['assets_fetched_at']})")
    try:
        result = SCANS[args.scan].run_scan(inputs, params, cache_dir=args.cache_dir)
    except (MissingAlpacaKeys, MissingBenchmark, *DATA_ERRORS) as exc:
        print(f"error: {exc}; nothing written", file=sys.stderr)
        return EXIT_FAILED

    baskets = list(result.values()) if isinstance(result, dict) else [result]
    for basket in baskets:
        print(f"{basket.scan_name} as of {basket.as_of}: {len(basket.members)} of {len(basket.universe)} passed, "
              f"{len(basket.rejected)} rejected (reasons in the basket)")
        for member in basket.members:
            print(f"  PASS {member.ticker:<6} score {member.score:+.4f}  close {member.metrics['close']:.2f}")
    for warning in baskets[0].parameters["warnings"]:
        print(f"warning: {warning}")
    for basket in baskets:
        print(f"wrote {save_basket(basket, args.out_dir)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
