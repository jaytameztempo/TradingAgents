"""SCAN-LongSidewaysChannel on the STRONG financial survivors. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_scanbot_long_channel --as-of 2026-09-30
    python -m extensions.scripts.run_scanbot_long_channel --financials-report path/to/financials_....json

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
from extensions.scanbot.funnel import DATA_ERRORS
from extensions.scanbot.long_channel import MIN_QUALITY_CHECKS, QUALITY_CHECKS, run_long_channel, save_report
from extensions.scripts.run_scanbot_trend import newest_financials_report

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SCAN-LongSidewaysChannel: STRONG names at channel support (no ranking, no orders)."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--financials-report", help="the step 2 financials report JSON to scan")
    source.add_argument("--as-of", help="use the newest financials report for this date, YYYY-MM-DD")
    parser.add_argument("--report-dir", help="where to look for financials reports (default: ~/.tradingagents/scanbot)")
    parser.add_argument("--cache-dir", help="reuse bars for completed ranges from this folder")
    parser.add_argument("--out-dir", help="folder for the report JSON (default: ~/.tradingagents/scanbot)")
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
        financials = load_financials(path)
        financials.gate_pass(STRONG)
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        print(f"error: cannot read a STRONG pass from financials report {path}: {exc!r}", file=sys.stderr)
        return EXIT_BAD_INPUT

    load_dotenv(find_dotenv(usecwd=True))
    print(f"financials report {path}: {len(financials.gate_pass(STRONG).survivors)} STRONG survivors as of {financials.as_of}")
    try:
        report = run_long_channel(financials, path, cache_dir=args.cache_dir)
    except (MissingAlpacaKeys, *DATA_ERRORS) as exc:
        print(f"error: {exc}; no report written", file=sys.stderr)
        return EXIT_FAILED

    print(f"{report.scan_name} as of {report.as_of}  (bars feed {report.policy['feed']}, STRONG pass)")
    print("  survivors after each stage:")
    for stage, count in report.stage_counts.items():
        print(f"    {stage:<22} {count:>6}")
    print("  fail reasons (a name can have several):")
    for code, count in report.reason_counts.items():
        print(f"    {code:<34} {count:>6}")
    print("  quality checks true on members (recorded, not required):")
    for q in QUALITY_CHECKS:
        print(f"    {q:<34} {report.quality_counts[q]:>6}")
    with_quality = sum(1 for s in report.members if report.calls[s]["quality_passed"] >= MIN_QUALITY_CHECKS)
    print(f"  members with >= {MIN_QUALITY_CHECKS} quality checks: {with_quality} (informational)")
    print(f"LONG_SIDE: {' '.join(report.members) or '-'}")
    for warning in report.warnings:
        print(f"warning: {warning}")

    out = save_report(report, args.out_dir)
    print(f"wrote {out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
