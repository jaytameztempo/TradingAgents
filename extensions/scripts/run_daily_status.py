"""Daily status: the market label, which bots are allowed, and the confirmed SIDE names. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_daily_status --as-of 2026-09-30

It reads the newest SPY+QQQ route, ranked side handoff and side 4-hour
confirmation for the date and writes one status JSON to
~/.tradingagents/status/ unless --out-dir is set. If the label is not SIDE, or
the three files do not chain together, the channel names are blocked. Without
a SPY+QQQ route nothing is written. It reads saved files only: no Alpaca call,
no bars, no orders.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from extensions.status.daily_status import (
    DOWN_BOT,
    SIDE_BOT,
    SIDES,
    UP_BOT,
    StatusInputError,
    save_status,
    status_for_date,
)

EXIT_OK, EXIT_BAD_INPUT = 0, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Daily status from saved route, handoff and 4-hour files (no orders).")
    parser.add_argument("--as-of", required=True, help="the date to report, YYYY-MM-DD")
    parser.add_argument("--route-dir", help="where to look for routes (default: ~/.tradingagents/routes)")
    parser.add_argument("--handoff-dir", help="where to look for handoffs (default: ~/.tradingagents/handoffs)")
    parser.add_argument("--report-dir", help="where to look for 4-hour reports (default: ~/.tradingagents/scanbot)")
    parser.add_argument("--out-dir", help="folder for the status JSON (default: ~/.tradingagents/status)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        status = status_for_date(as_of, args.route_dir, args.handoff_dir, args.report_dir)
    except StatusInputError as exc:
        print(f"error: {exc}; no status written", file=sys.stderr)
        return EXIT_BAD_INPUT

    labels = ", ".join(f"{s} {label or 'missing'}" for s, label in status.regime_labels.items())
    print(f"daily status as of {status.as_of}")
    print(f"  market label: {status.market_label}  ({status.regime_symbol}: {labels})")
    for bot in (UP_BOT, DOWN_BOT):
        names = f" for {', '.join(status.trend_names) or '(no passed names)'}" if status.bots[bot] else ""
        print(f"  {bot + ':':<9}{'ALLOWED' if status.bots[bot] else 'BLOCKED'}{names}")
    print(f"  {SIDE_BOT + ':':<9}{'ALLOWED' if status.bots[SIDE_BOT] else 'BLOCKED'}")
    for side in SIDES:
        shown = ' '.join(status.confirmed[side]) or '-'
        print(f"  confirmed {side + ':':<6} {shown}{' (blocked)' if status.channel_names_blocked else ''}")
    for reason in status.blocked_reasons:
        print(f"  blocked: {reason}")
    for name, path in status.sources.items():
        print(f"  {name}: {path or '(none)'}")
    print("  read only; no order placed")
    for warning in status.warnings:
        print(f"warning: {warning}")

    out = save_status(status, args.out_dir)
    print(f"wrote {out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
