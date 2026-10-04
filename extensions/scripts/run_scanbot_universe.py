"""SCANBot build step 1: universe and liquidity on Alpaca, with a count for every gate. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_scanbot_universe --as-of 2026-09-30
    python -m extensions.scripts.run_scanbot_universe --as-of 2026-09-30 --preset CONSERVATIVE
    python -m extensions.scripts.run_scanbot_universe --as-of 2026-09-30 --assets-file assets.json

Without --assets-file it fetches Alpaca's asset list with one read-only GET to the
paper host and saves a snapshot to ~/.tradingagents/assets/ (or --assets-dir).
Bars and quotes use the SIP feed. If Alpaca refuses SIP bars, nothing is written.
If quotes fail, the report is written with spread_method not_applied.
The report goes to ~/.tradingagents/scanbot/ unless --out-dir is set.
Keys are read from ALPACA_API_KEY and ALPACA_SECRET_KEY, in the environment or in .env.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import find_dotenv, load_dotenv

from extensions.market_data import alpaca_assets
from extensions.market_data.alpaca_bars import MissingAlpacaKeys, validate_date_range
from extensions.scanbot import funnel
from extensions.scanbot.funnel import DATA_ERRORS, PRESETS, CalendarError, save_report

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SCANBot step 1: universe and liquidity gates on Alpaca, with per-gate counts (no orders)."
    )
    parser.add_argument("--as-of", required=True, help="date for the bars and sessions, YYYY-MM-DD (not after today)")
    parser.add_argument("--preset", default="DEFAULT", choices=list(PRESETS), help="liquidity floors (default: DEFAULT)")
    parser.add_argument("--assets-file", help="use this saved asset snapshot instead of fetching the list")
    parser.add_argument("--assets-dir", help="folder for a fetched asset snapshot (default: ~/.tradingagents/assets)")
    parser.add_argument("--cache-dir", help="reuse bars for completed ranges from this folder")
    parser.add_argument("--out-dir", help="folder for the report JSON (default: ~/.tradingagents/scanbot)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _, as_of = validate_date_range(args.as_of, args.as_of)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    if args.assets_file:
        try:
            fetched_at, assets = alpaca_assets.load_snapshot(args.assets_file)
        except (OSError, ValueError, KeyError) as exc:
            print(f"error: cannot read asset snapshot {args.assets_file}: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT
        source = args.assets_file
    else:
        load_dotenv(find_dotenv(usecwd=True))
        try:
            assets = alpaca_assets.fetch_assets()
        except (MissingAlpacaKeys, *DATA_ERRORS) as exc:
            print(f"error: {exc}; no report written", file=sys.stderr)
            return EXIT_FAILED
        snapshot = alpaca_assets.save_snapshot(assets, args.assets_dir)
        fetched_at, _ = alpaca_assets.load_snapshot(snapshot)
        source = str(snapshot)
        print(f"asset snapshot: {len(assets)} records saved to {snapshot}")

    load_dotenv(find_dotenv(usecwd=True))
    try:
        report = funnel.run_funnel(as_of, assets, fetched_at, source, args.preset, cache_dir=args.cache_dir)
    except (MissingAlpacaKeys, CalendarError, *DATA_ERRORS) as exc:
        print(f"error: {exc}; no report written", file=sys.stderr)
        return EXIT_FAILED

    print(f"SCANBot universe and liquidity as of {as_of} (preset {report.preset}, bars feed {report.feed})")
    print(f"  {report.universe_count} asset records in")
    for count in report.stage_counts:
        label = f"{count.stage}/{count.gate}"
        if count.applied:
            print(f"  {label:<24} removed {count.removed:>6}   survived {count.survived:>6}")
        else:
            print(f"  {label:<24} NOT APPLIED          survived {count.survived:>6}")
    print(f"survivors: {len(report.survivors)}  (spread_method {report.spread_method})")
    for warning in report.warnings:
        print(f"warning: {warning}")

    path = save_report(report, args.out_dir)
    print(f"wrote {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
