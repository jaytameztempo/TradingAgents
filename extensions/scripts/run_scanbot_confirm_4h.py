"""SCANBot 4-hour confirmation of the daily pivot passes in a pivot report. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_scanbot_confirm_4h --as-of 2026-09-30
    python -m extensions.scripts.run_scanbot_confirm_4h --pivot-report path/to/pivot_....json

--as-of picks the newest pivot report for that date in --report-dir. Only the
report's passed names are checked; daily fails stay fails. Bars are
regular-session 4-hour bars built from SIP 30-minute bars dated on or before
the as-of date, with no extended hours and no hourly bars. If Alpaca refuses
them, nothing is written. The report goes to ~/.tradingagents/scanbot/ unless
--out-dir is set. Keys are read from ALPACA_API_KEY and ALPACA_SECRET_KEY, in
the environment or in .env.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from extensions.market_data.alpaca_bars import MissingAlpacaKeys
from extensions.scanbot.confirm_4h import REASONS, run_confirm_4h, save_report
from extensions.scanbot.funnel import DATA_ERRORS, CalendarError, default_report_dir
from extensions.scanbot.pivot import SIDES
from extensions.scanbot.pivot import load_report as load_pivot

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2
READ_ERRORS = (OSError, ValueError, KeyError, TypeError)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SCANBot: 4-hour confirmation of daily pivot passes (no orders)."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pivot-report", help="the pivot report JSON whose passes to check")
    source.add_argument("--as-of", help="use the newest pivot report for this date, YYYY-MM-DD")
    parser.add_argument("--report-dir", help="where to look for pivot reports (default: ~/.tradingagents/scanbot)")
    parser.add_argument("--cache-dir", help="reuse bars for completed ranges from this folder")
    parser.add_argument("--out-dir", help="folder for the report JSON (default: ~/.tradingagents/scanbot)")
    return parser


def newest_pivot_report(as_of: date, folder: str | Path | None = None) -> Path | None:
    folder = Path(folder) if folder is not None else default_report_dir()
    found = sorted(folder.glob(f"pivot_{as_of.isoformat()}_*.json"))
    return found[-1] if found else None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.pivot_report:
        path = Path(args.pivot_report)
    else:
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
            return EXIT_BAD_INPUT
        path = newest_pivot_report(as_of, args.report_dir)
        if path is None:
            print(f"error: no pivot report for {as_of}; run run_scanbot_pivot first", file=sys.stderr)
            return EXIT_BAD_INPUT
    try:
        pivot = load_pivot(path)
    except READ_ERRORS as exc:
        print(f"error: cannot read pivot report {path}: {exc!r}", file=sys.stderr)
        return EXIT_BAD_INPUT

    load_dotenv(find_dotenv(usecwd=True))
    print(f"pivot report {path}: daily pass {len(pivot.passed[SIDES[0]])} {SIDES[0]}, "
          f"{len(pivot.passed[SIDES[1]])} {SIDES[1]} as of {pivot.as_of}")
    try:
        report = run_confirm_4h(pivot, path, cache_dir=args.cache_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except (MissingAlpacaKeys, CalendarError, *DATA_ERRORS) as exc:
        print(f"error: {exc}; no report written", file=sys.stderr)
        return EXIT_FAILED

    print(f"SCANBot 4-hour confirmation as of {report.as_of}  (regular session 4h from 30m, feed {report.policy['feed']})")
    for side in SIDES:
        c = report.counts[side]
        print(f"  {side}: daily pass {c['daily_pass']} checked, 4-hour pass {c['pass_4h']}, fail {c['fail_4h']}"
              f"  (daily fails carried, not checked: {c['daily_fail_carried']})")
        print("    4-hour fail reasons (a name can have several):")
        for code in REASONS:
            print(f"      {code:<34} {c['reason_counts'][code]:>4}")
        for symbol, call in report.calls[side].items():
            print(f"    {symbol}: {'PASS' if call['confirm_pass'] else 'FAIL'}  {'; '.join(call['detail'])}")
        print(f"    CONFIRMED: {' '.join(report.confirmed[side]) or '-'}")
    for warning in report.warnings:
        print(f"warning: {warning}")

    out = save_report(report, args.out_dir)
    print(f"wrote {out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
