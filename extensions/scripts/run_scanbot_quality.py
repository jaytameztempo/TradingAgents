"""SCANBot pivot quality filter on the 4-hour confirmed names. It fetches no bars and places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_scanbot_quality --as-of 2026-09-30
    python -m extensions.scripts.run_scanbot_quality --confirm-report path/to/confirm4h_....json

--as-of picks the newest 4-hour report for that date in --report-dir. The pivot
report is the one that 4-hour report names, unless --pivot-report is set; it
must be the same report the 4-hour step used. A name passes on at least two of
the four quality checks recorded in the pivot report. Daily and 4-hour fails
stay fails. The report goes to ~/.tradingagents/scanbot/ unless --out-dir is set.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from extensions.scanbot.confirm_4h import load_report as load_confirm
from extensions.scanbot.funnel import default_report_dir
from extensions.scanbot.pivot import QUALITY_CHECKS, SIDES
from extensions.scanbot.pivot import load_report as load_pivot
from extensions.scanbot.quality import REASONS, run_quality, save_report

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2
READ_ERRORS = (OSError, ValueError, KeyError, TypeError)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SCANBot: pivot quality filter on 4-hour confirmed names (no bars, no orders)."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--confirm-report", help="the 4-hour report JSON whose confirmed names to filter")
    source.add_argument("--as-of", help="use the newest 4-hour report for this date, YYYY-MM-DD")
    parser.add_argument("--pivot-report", help="the pivot report JSON (default: the one the 4-hour report names)")
    parser.add_argument("--report-dir", help="where to look for 4-hour reports (default: ~/.tradingagents/scanbot)")
    parser.add_argument("--out-dir", help="folder for the report JSON (default: ~/.tradingagents/scanbot)")
    return parser


def newest_confirm_report(as_of: date, folder: str | Path | None = None) -> Path | None:
    folder = Path(folder) if folder is not None else default_report_dir()
    found = sorted(folder.glob(f"confirm4h_{as_of.isoformat()}_*.json"))
    return found[-1] if found else None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.confirm_report:
        confirm_path = Path(args.confirm_report)
    else:
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
            return EXIT_BAD_INPUT
        confirm_path = newest_confirm_report(as_of, args.report_dir)
        if confirm_path is None:
            print(f"error: no 4-hour report for {as_of}; run run_scanbot_confirm_4h first", file=sys.stderr)
            return EXIT_BAD_INPUT
    try:
        confirm = load_confirm(confirm_path)
    except READ_ERRORS as exc:
        print(f"error: cannot read 4-hour report {confirm_path}: {exc!r}", file=sys.stderr)
        return EXIT_BAD_INPUT
    pivot_path = Path(args.pivot_report or confirm.pivot_report)
    try:
        pivot = load_pivot(pivot_path)
    except READ_ERRORS as exc:
        print(f"error: cannot read pivot report {pivot_path}: {exc!r}", file=sys.stderr)
        return EXIT_BAD_INPUT

    print(f"4-hour report {confirm_path}: confirmed {len(confirm.confirmed[SIDES[0]])} {SIDES[0]}, "
          f"{len(confirm.confirmed[SIDES[1]])} {SIDES[1]} as of {confirm.as_of}")
    print(f"pivot report {pivot_path}")
    try:
        report = run_quality(confirm, confirm_path, pivot, pivot_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    print(f"SCANBot quality filter as of {report.as_of}  (at least {report.policy['min_checks']} of "
          f"{len(QUALITY_CHECKS)} recorded checks)")
    for side in SIDES:
        c = report.counts[side]
        print(f"  {side}: checked {c['checked_quality']}, quality pass {c['quality_pass']}, fail {c['quality_fail']}"
              f"  (carried, not checked: daily fails {c['daily_fail_carried']}, 4-hour fails {c['fail_4h_carried']})")
        print("    quality fail reasons:")
        for code in REASONS:
            print(f"      {code:<34} {c['reason_counts'][code]:>4}")
        for symbol, call in report.calls[side].items():
            print(f"    {symbol}: {'PASS' if call['quality_pass'] else 'FAIL'}  {'; '.join(call['detail'])}")
        print(f"    PASSED: {' '.join(report.passed[side]) or '-'}")
        print(f"    final: pass {c['final_pass']}, fail {c['final_fail']} of {c['daily_entered']} entered")
    for warning in report.warnings:
        print(f"warning: {warning}")

    out = save_report(report, args.out_dir)
    print(f"wrote {out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
