"""Hand the sideways channel members to research, gated by the route's market regime. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_side_handoff --as-of 2026-09-30

Reads the newest long channel and short channel reports in ~/.tradingagents/scanbot/
and the newest route in ~/.tradingagents/routes/ for the date (or --report-dir
and --route-dir). Under a SIDE market every long channel member gets a long
fade off support plan and every short channel member a short fade off
resistance plan in ~/.tradingagents/plans/. Under UP or DOWN every name is
recorded as blocked with no plan. The handoff goes to ~/.tradingagents/handoffs/
unless --out-dir is set. Nothing here calls Alpaca.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from extensions.research.side_handoff import save_side_handoff, side_handoff_for_date
from extensions.router.strategy_router import MissingInput

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hand sideways channel members to research under a SIDE market (no orders)."
    )
    parser.add_argument("--as-of", required=True, help="date of the channel reports and route, YYYY-MM-DD")
    parser.add_argument("--report-dir", help="folder of channel reports (default: ~/.tradingagents/scanbot)")
    parser.add_argument("--route-dir", help="folder of route decisions (default: ~/.tradingagents/routes)")
    parser.add_argument("--out-dir", help="folder for the handoff JSON (default: ~/.tradingagents/handoffs)")
    parser.add_argument("--plan-dir", help="folder for research plans (default: ~/.tradingagents/plans)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        handoff = side_handoff_for_date(as_of, args.report_dir, args.route_dir)
    except (MissingInput, ValueError) as exc:
        print(f"error: {exc}; no handoff written", file=sys.stderr)
        return EXIT_FAILED

    print(f"long channel report: {handoff.long_channel_report}")
    print(f"short channel report: {handoff.short_channel_report}")
    print(f"route: {handoff.route_file}")
    print(f"market regime ({handoff.regime_symbol}): {handoff.regime_label}")
    path, plans = save_side_handoff(handoff, args.out_dir, args.plan_dir)
    for symbol, name in handoff.names.items():
        print(f"  {symbol} ({name['setup']}): {name['status']}  {'; '.join(name['reasons'])}")
    for warning in handoff.warnings:
        print(f"warning: {warning}")
    for plan in plans:
        print(f"wrote plan {plan}")
    print(f"plans written: {len(plans)}; no order placed")
    print(f"wrote {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
