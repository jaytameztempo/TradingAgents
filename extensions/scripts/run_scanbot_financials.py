"""SCANBot build step 2: the strong and weak financial gates over a saved universe report. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_scanbot_financials --as-of 2026-09-30
    python -m extensions.scripts.run_scanbot_financials --universe-report path/to/universe_....json
    python -m extensions.scripts.run_scanbot_financials --as-of 2026-09-30 --offline

--as-of picks the newest universe report for that date in --report-dir.
Fundamentals come from SEC EDGAR company facts (free, no key), cached one file
per symbol in ~/.tradingagents/fundamentals/ (or --fundamentals-dir). A file
under 7 days old is not refetched; one over 10 days old fails that symbol closed.
--offline uses the cache only. Set SEC_EDGAR_USER_AGENT to your name and email
so SEC can reach you about your traffic. The report goes to
~/.tradingagents/scanbot/ unless --out-dir is set.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from extensions.scanbot import fundamentals as fx
from extensions.scanbot.financials import run_financials, save_report
from extensions.scanbot.funnel import default_report_dir, load_report

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SCANBot step 2: strong and weak financial gates on SEC EDGAR facts (no orders)."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--universe-report", help="the step 1 universe report JSON to gate")
    source.add_argument("--as-of", help="use the newest universe report for this date, YYYY-MM-DD")
    parser.add_argument("--report-dir", help="where to look for universe reports (default: ~/.tradingagents/scanbot)")
    parser.add_argument("--fundamentals-dir", help="per-symbol fundamentals cache (default: ~/.tradingagents/fundamentals)")
    parser.add_argument("--out-dir", help="folder for the report JSON (default: ~/.tradingagents/scanbot)")
    parser.add_argument("--offline", action="store_true", help="use cached fundamentals only; fetch nothing")
    return parser


def newest_universe_report(as_of: date, folder: str | Path | None = None) -> Path | None:
    folder = Path(folder) if folder is not None else default_report_dir()
    found = sorted(folder.glob(f"universe_{as_of.isoformat()}_*.json"))
    return found[-1] if found else None


def _progress(done: int, total: int) -> None:
    if done % 100 == 0 or done == total:
        print(f"  fundamentals {done}/{total}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.universe_report:
        path = Path(args.universe_report)
    else:
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
            return EXIT_BAD_INPUT
        path = newest_universe_report(as_of, args.report_dir)
        if path is None:
            print(f"error: no universe report for {as_of}; run run_scanbot_universe first", file=sys.stderr)
            return EXIT_BAD_INPUT
    try:
        universe = load_report(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: cannot read universe report {path}: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    load_dotenv(find_dotenv(usecwd=True))
    print(f"universe report {path}: {len(universe.survivors)} liquidity survivors as of {universe.as_of}")
    try:
        lookups = fx.load_fundamentals(
            universe.survivors, folder=args.fundamentals_dir, offline=args.offline, progress=_progress
        )
    except fx.TickerMapUnavailable as exc:
        print(f"error: SEC ticker map unavailable: {exc}; no report written", file=sys.stderr)
        return EXIT_FAILED

    report = run_financials(universe, path, lookups, fundamentals_dir=args.fundamentals_dir)
    sources = ", ".join(f"{k} {v}" for k, v in report.data_sources.items())
    print(f"SCANBot financial gates as of {report.as_of}  (fundamentals: {sources})")
    for gate_pass in report.passes:
        print(f"{gate_pass.financial_gate} pass: {report.universe_survivors} in")
        for count in gate_pass.stage_counts:
            print(f"  {count.gate:<20} removed {count.removed:>6}   survived {count.survived:>6}")
        print(f"  survivors: {len(gate_pass.survivors)}")
    if report.warnings:
        print(f"warnings: {len(report.warnings)} (listed in the report)")

    out = save_report(report, args.out_dir)
    print(f"wrote {out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
