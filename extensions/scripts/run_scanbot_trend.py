"""SCANBot build step 3: the trend classifier on the STRONG financial survivors. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_scanbot_trend --as-of 2026-09-30
    python -m extensions.scripts.run_scanbot_trend --financials-report path/to/financials_....json

--as-of picks the newest financials report for that date in --report-dir.
Bars are split-adjusted SIP daily bars dated on or before the report's as-of
date. If Alpaca refuses them, nothing is written. The report goes to
~/.tradingagents/scanbot/ unless --out-dir is set. Keys are read from
ALPACA_API_KEY and ALPACA_SECRET_KEY, in the environment or in .env.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from extensions.market_data.alpaca_bars import MissingAlpacaKeys
from extensions.scanbot.financials import STRONG
from extensions.scanbot.financials import load_report as load_financials
from extensions.scanbot.funnel import DATA_ERRORS, default_report_dir
from extensions.scanbot.trend import run_trend, save_report

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SCANBot step 3: UP / DOWN / UNCLASSIFIED trend labels on STRONG survivors (no orders)."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--financials-report", help="the step 2 financials report JSON to classify")
    source.add_argument("--as-of", help="use the newest financials report for this date, YYYY-MM-DD")
    parser.add_argument("--report-dir", help="where to look for financials reports (default: ~/.tradingagents/scanbot)")
    parser.add_argument("--cache-dir", help="reuse bars for completed ranges from this folder")
    parser.add_argument("--out-dir", help="folder for the report JSON (default: ~/.tradingagents/scanbot)")
    return parser


def newest_financials_report(as_of: date, folder: str | Path | None = None) -> Path | None:
    folder = Path(folder) if folder is not None else default_report_dir()
    found = sorted(folder.glob(f"financials_{as_of.isoformat()}_*.json"))
    return found[-1] if found else None


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
        financials = load_financials(path)
        financials.gate_pass(STRONG)
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        print(f"error: cannot read a STRONG pass from financials report {path}: {exc!r}", file=sys.stderr)
        return EXIT_BAD_INPUT

    load_dotenv(find_dotenv(usecwd=True))
    print(f"financials report {path}: {len(financials.gate_pass(STRONG).survivors)} STRONG survivors as of {financials.as_of}")
    try:
        report = run_trend(financials, path, cache_dir=args.cache_dir)
    except (MissingAlpacaKeys, *DATA_ERRORS) as exc:
        print(f"error: {exc}; no report written", file=sys.stderr)
        return EXIT_FAILED

    print(f"SCANBot trend classifier as of {report.as_of}  (bars feed {report.policy['feed']}, STRONG pass)")
    print(f"  entered {report.entered}")
    for label, count in report.label_counts.items():
        print(f"  {label:<14} {count:>6}")
    print("  unclassified reasons (a name can have several):")
    for code, count in report.reason_counts.items():
        print(f"    {code:<22} {count:>6}")
    print(f"  aligned with ADX >= 25 but tangled: {report.tangled_only}")
    print(f"UP:   {' '.join(report.up) or '-'}")
    print(f"DOWN: {' '.join(report.down) or '-'}")
    for warning in report.warnings:
        print(f"warning: {warning}")

    out = save_report(report, args.out_dir)
    print(f"wrote {out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
