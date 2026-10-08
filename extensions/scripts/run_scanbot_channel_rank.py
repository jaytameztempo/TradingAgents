"""Rank and cap the saved SIDE channel baskets for one as_of. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_scanbot_channel_rank --as-of 2026-09-30

Reads the newest long_channel_ and short_channel_ reports for that date in
--report-dir, the financials report they name, and the universe report that
names. No network. Long names: top 15, at most 4 per sector (operator-supplied
sectors). Short names: all kept and ranked. The report goes to
~/.tradingagents/scanbot/ unless --out-dir is set.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from extensions.scanbot.channel_rank import EMITTED, SECTOR_CAP, rank_channels, save_report
from extensions.scanbot.funnel import default_report_dir

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def newest_report(prefix: str, as_of: date, folder: str | Path | None = None) -> Path | None:
    folder = Path(folder) if folder is not None else default_report_dir()
    found = sorted(folder.glob(f"{prefix}_{as_of.isoformat()}_*.json"))
    return found[-1] if found else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rank and cap the saved SIDE channel baskets (no orders).")
    parser.add_argument("--as-of", required=True, help="channel reports for this date, YYYY-MM-DD")
    parser.add_argument("--report-dir", help="where to look for reports (default: ~/.tradingagents/scanbot)")
    parser.add_argument("--out-dir", help="folder for the rank report JSON (default: ~/.tradingagents/scanbot)")
    return parser


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _print_rows(rows: list[dict], components: list[str]) -> None:
    head = " ".join(f"{c[:6]:>6}" for c in components)
    print(f"    {'#':>2} {'sym':<5} {'sector':<23} {'score':>6} {head}  status")
    for r in rows:
        parts = " ".join(f"{r['components'][c]:6.2f}" for c in components)
        print(f"    {r['rank']:>2} {r['symbol']:<5} {r['sector']:<23} {r['score']:6.2f} {parts}  {r['status']}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
        return EXIT_BAD_INPUT
    long_path = newest_report("long_channel", as_of, args.report_dir)
    short_path = newest_report("short_channel", as_of, args.report_dir)
    if long_path is None or short_path is None:
        print(f"error: need both a long_channel and a short_channel report for {as_of}", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        long_report, short_report = _load(long_path), _load(short_path)
        fin_path = Path(long_report["financials_report"])
        financials = _load(fin_path)
        uni_path = Path(financials["universe_report"])
        universe = _load(uni_path)
        report = rank_channels(
            long_report,
            short_report,
            financials,
            universe,
            sources={
                "long_channel": str(long_path),
                "short_channel": str(short_path),
                "financials": str(fin_path),
                "universe": str(uni_path),
            },
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: cannot rank the {as_of} channel reports: {exc!r}; no report written", file=sys.stderr)
        return EXIT_FAILED

    print(f"channel rank as of {report.as_of}  (sectors {report.policy['sector_source']}, cap {SECTOR_CAP} per sector)")
    print(f"  long  {long_path.name}: {len(report.long_ranked)} scored")
    _print_rows(report.long_ranked, list(report.policy["long_weights"]))
    print(f"  short {short_path.name}: {len(report.short_ranked)} scored, all kept")
    _print_rows(report.short_ranked, list(report.policy["short_weights"]))
    print(f"  emitted sectors: {report.sector_counts}")
    print(f"LONG_SIDE ({sum(r['status'] == EMITTED for r in report.long_ranked)}): {' '.join(report.long_emitted) or '-'}")
    print(f"SHORT_SIDE ({len(report.short_kept)}): {' '.join(report.short_kept) or '-'}")
    for warning in report.warnings:
        print(f"warning: {warning}")

    out = save_report(report, args.out_dir)
    print(f"wrote {out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
