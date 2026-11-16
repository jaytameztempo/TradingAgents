"""Rebuild the pipeline for past dates that never had a scan run, for the Backtest page. Places no order.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_backtest_reconstruct --start 2026-10-13 --end 2026-10-23
    python -m extensions.scripts.run_backtest_reconstruct --start 2026-10-12 --end 2026-10-12 --all-dates --rebuild

Weekdays with a saved route are replayed by the backtest and are skipped here unless --all-dates is given (to compare
a rebuild with what was saved). A date already rebuilt is skipped unless --rebuild is given. Missing daily bars are
fetched (Alpaca paper keys, ALPACA_API_KEY and ALPACA_SECRET_KEY, in the environment or in .env) and cached; the
reports go to <home>/backtest/reconstructed, never to the live folders. With --fetch-fundamentals, SEC fundamentals
missing from the cache are downloaded the way the live financials step does it (SEC_EDGAR_USER_AGENT in .env) and
saved in the shared fundamentals cache.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from extensions.backtest import reconstruct as rc
from extensions.market_data.alpaca_bars import MissingAlpacaKeys, market_today
from extensions.scanbot.funnel import DATA_ERRORS

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2
MAX_RANGE_DAYS = 366


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rebuild the pipeline for past dates (backtest; no orders).")
    parser.add_argument("--start", required=True, help="first as-of date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="last as-of date, YYYY-MM-DD (before today)")
    parser.add_argument("--home", default=str(Path.home() / ".tradingagents"), help="the data folder")
    parser.add_argument("--rebuild", action="store_true", help="rebuild dates that were already rebuilt")
    parser.add_argument("--all-dates", action="store_true", help="also rebuild dates that have saved files")
    parser.add_argument("--fetch-fundamentals", action="store_true",
                        help="download missing SEC fundamentals the live way (needs SEC_EDGAR_USER_AGENT; slower)")
    return parser


def _log(message: str) -> None:
    print(message, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    except ValueError:
        print("error: --start and --end must be YYYY-MM-DD", file=sys.stderr)
        return EXIT_BAD_INPUT
    today = market_today()
    if end < start or end >= today or (end - start).days >= MAX_RANGE_DAYS:
        print(f"error: need start <= end < today ({today}) and at most {MAX_RANGE_DAYS} days", file=sys.stderr)
        return EXIT_BAD_INPUT
    home = Path(args.home)

    dates = rc.pending_dates(home, start, end, rebuild=args.rebuild, all_dates=args.all_dates)
    if not dates:
        _log(f"nothing to rebuild from {start} to {end}: every weekday is saved or already rebuilt")
        return EXIT_OK
    _log(f"rebuilding {len(dates)} date(s): {dates[0]} .. {dates[-1]}")

    load_dotenv(find_dotenv(usecwd=True))
    try:
        snapshot = rc.newest_snapshot(home)
        from extensions.market_data import alpaca_assets
        _, assets = alpaca_assets.load_snapshot(snapshot)
        symbols = sorted({*rc.universe_symbols(assets), *rc.REGIME_SYMBOLS})
        _log(f"asset snapshot {snapshot.name} (not point in time): {len(symbols)} symbols pass the asset gates")
        fetch_end = today - timedelta(days=1)  # bars up to yesterday, so held trades have bars to exit on
        frames = rc.ensure_bars(home, symbols, dates[0] - timedelta(days=rc.LOOKBACK_DAYS), fetch_end, progress=_log)
    except rc.ReconstructError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except (MissingAlpacaKeys, *DATA_ERRORS) as exc:
        print(f"error: fetching daily bars failed: {exc}", file=sys.stderr)
        return EXIT_FAILED

    ctx = rc.build_context(home, rc.master_frame(frames), log=_log, fetch_fundamentals=args.fetch_fundamentals)
    _log(f"SEC fundamentals: {'download missing files' if args.fetch_fundamentals else 'cached files only'}")
    manifest = rc.read_manifest(home)
    failed = 0
    for i, as_of in enumerate(dates, 1):
        _log(f"[{i}/{len(dates)}] {as_of}")
        if as_of.isoformat() in manifest["dates"]:
            rc.forget_date(home, manifest, as_of)
        try:
            manifest["dates"][as_of.isoformat()] = rc.reconstruct_date(as_of, ctx)
        except (rc.ReconstructError, MissingAlpacaKeys, *DATA_ERRORS, ValueError) as exc:
            failed += 1
            _log(f"  {as_of}: not rebuilt: {exc}")
            continue
        finally:
            rc.write_manifest(home, manifest)
        entry = manifest["dates"][as_of.isoformat()]
        if entry["status"] == rc.HOLIDAY:
            _log(f"  {as_of}: market holiday")
        for warning in entry["warnings"]:
            _log(f"  warning: {warning}")
    _log(f"done: {len(dates) - failed} rebuilt, {failed} failed; reports in {rc.recon_home(home)}; no order placed")
    return EXIT_FAILED if failed == len(dates) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
