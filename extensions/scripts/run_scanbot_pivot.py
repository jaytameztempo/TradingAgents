"""SCANBot build step 4: pivot structure on the UP and DOWN names of a trend report. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_scanbot_pivot --as-of 2026-09-30
    python -m extensions.scripts.run_scanbot_pivot --trend-report path/to/trend_....json

--as-of picks the newest trend report for that date in --report-dir.
easy_to_borrow for DOWN names comes from the asset snapshot behind the trend
report (trend -> financials -> universe -> assets) unless --assets-snapshot is
set. Bars are split-adjusted SIP daily bars dated on or before the as-of date.
If Alpaca refuses them, nothing is written. The report goes to
~/.tradingagents/scanbot/ unless --out-dir is set. Keys are read from
ALPACA_API_KEY and ALPACA_SECRET_KEY, in the environment or in .env.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from extensions.market_data.alpaca_assets import load_snapshot
from extensions.market_data.alpaca_bars import MissingAlpacaKeys
from extensions.scanbot.financials import load_report as load_financials
from extensions.scanbot.funnel import DATA_ERRORS, default_report_dir
from extensions.scanbot.funnel import load_report as load_universe
from extensions.scanbot.pivot import MIN_QUALITY_CHECKS, QUALITY_CHECKS, SIDES, borrow_flags, run_pivot, save_report
from extensions.scanbot.trend import load_report as load_trend

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2
READ_ERRORS = (OSError, ValueError, KeyError, TypeError)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SCANBot step 4: bottom / top pivot structure on UP and DOWN names (no orders)."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--trend-report", help="the step 3 trend report JSON to check")
    source.add_argument("--as-of", help="use the newest trend report for this date, YYYY-MM-DD")
    parser.add_argument("--report-dir", help="where to look for trend reports (default: ~/.tradingagents/scanbot)")
    parser.add_argument("--assets-snapshot", help="asset snapshot for easy_to_borrow (default: the one behind the trend report)")
    parser.add_argument("--cache-dir", help="reuse bars for completed ranges from this folder")
    parser.add_argument("--out-dir", help="folder for the report JSON (default: ~/.tradingagents/scanbot)")
    return parser


def newest_trend_report(as_of: date, folder: str | Path | None = None) -> Path | None:
    folder = Path(folder) if folder is not None else default_report_dir()
    found = sorted(folder.glob(f"trend_{as_of.isoformat()}_*.json"))
    return found[-1] if found else None


def snapshot_behind(trend_financials_report: str) -> Path:
    """The asset snapshot the universe report used, found through the financials report."""
    financials = load_financials(trend_financials_report)
    return Path(load_universe(financials.universe_report).assets_source)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.trend_report:
        path = Path(args.trend_report)
    else:
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
            return EXIT_BAD_INPUT
        path = newest_trend_report(as_of, args.report_dir)
        if path is None:
            print(f"error: no trend report for {as_of}; run run_scanbot_trend first", file=sys.stderr)
            return EXIT_BAD_INPUT
    try:
        trend = load_trend(path)
    except READ_ERRORS as exc:
        print(f"error: cannot read trend report {path}: {exc!r}", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        snapshot = Path(args.assets_snapshot) if args.assets_snapshot else snapshot_behind(trend.financials_report)
        fetched_at, assets = load_snapshot(snapshot)
    except READ_ERRORS as exc:
        print(f"error: cannot read the asset snapshot for easy_to_borrow: {exc!r}", file=sys.stderr)
        return EXIT_BAD_INPUT

    load_dotenv(find_dotenv(usecwd=True))
    print(f"trend report {path}: {len(trend.up)} UP, {len(trend.down)} DOWN as of {trend.as_of}")
    print(f"easy_to_borrow from {snapshot} (fetched {fetched_at.isoformat()})")
    try:
        report = run_pivot(trend, path, borrow_flags(assets), str(snapshot), fetched_at, cache_dir=args.cache_dir)
    except (MissingAlpacaKeys, *DATA_ERRORS) as exc:
        print(f"error: {exc}; no report written", file=sys.stderr)
        return EXIT_FAILED

    print(f"SCANBot pivot structure as of {report.as_of}  (bars feed {report.policy['feed']}, quality recorded, not required)")
    for side in SIDES:
        c = report.counts[side]
        print(f"  {side}: entered {c['entered']}, structure pass {c['structure_pass']}, fail {c['structure_fail']}")
        print("    fail reasons (a name can have several):")
        for code, count in c["reason_counts"].items():
            print(f"      {code:<30} {count:>4}")
        print("    quality checks true, on structure passes:")
        for q in QUALITY_CHECKS:
            print(f"      {q:<30} {c['quality_counts_pass'][q]:>4}")
        print(f"    structure passes with >= {MIN_QUALITY_CHECKS} quality checks: "
              f"{c[f'pass_with_{MIN_QUALITY_CHECKS}_quality']} (informational)")
        print(f"    PASS: {' '.join(report.passed[side]) or '-'}")
    for warning in report.warnings:
        print(f"warning: {warning}")

    out = save_report(report, args.out_dir)
    print(f"wrote {out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
