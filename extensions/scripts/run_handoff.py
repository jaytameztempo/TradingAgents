"""Hand the 4-hour confirmed names to research, gated by the route's market regime. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_handoff --as-of 2026-09-30 --delisted QRVO:2026-10-05

Reads the newest 4-hour report in ~/.tradingagents/scanbot/ and the newest route
in ~/.tradingagents/routes/ for the date (or --report-dir and --route-dir). A
name whose side matches the market regime gets a research plan in
~/.tradingagents/plans/; every other name is recorded as blocked with no plan.
A --delisted name never gets a plan. The handoff goes to
~/.tradingagents/handoffs/ unless --out-dir is set. Nothing here calls Alpaca.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from extensions.research.handoff import handoff_for_date, parse_delisted, save_handoff
from extensions.router.strategy_router import MissingInput

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hand confirmed names to research by the route's market regime (no orders)."
    )
    parser.add_argument("--as-of", required=True, help="date of the 4-hour report and route, YYYY-MM-DD")
    parser.add_argument("--delisted", action="append", metavar="SYMBOL:YYYY-MM-DD",
                        help="a name delisted after this date; it gets no plan (repeatable)")
    parser.add_argument("--report-dir", help="folder of 4-hour reports (default: ~/.tradingagents/scanbot)")
    parser.add_argument("--route-dir", help="folder of route decisions (default: ~/.tradingagents/routes)")
    parser.add_argument("--out-dir", help="folder for the handoff JSON (default: ~/.tradingagents/handoffs)")
    parser.add_argument("--plan-dir", help="folder for research plans (default: ~/.tradingagents/plans)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        as_of = date.fromisoformat(args.as_of)
        delisted = parse_delisted(args.delisted)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        handoff = handoff_for_date(as_of, delisted, args.report_dir, args.route_dir)
    except (MissingInput, ValueError) as exc:
        print(f"error: {exc}; no handoff written", file=sys.stderr)
        return EXIT_FAILED

    print(f"4-hour report: {handoff.confirm4h_report}")
    print(f"route: {handoff.route_file}")
    print(f"market regime ({handoff.regime_symbol}): {handoff.regime_label}")
    path, plans = save_handoff(handoff, args.out_dir, args.plan_dir)
    for symbol, name in handoff.names.items():
        flag = f"  [delisted after {name['delisted']['after']}]" if name["delisted"] else ""
        print(f"  {symbol} ({name['side']}): {name['status']}{flag}  {'; '.join(name['reasons'])}")
    if handoff.route_passers_not_confirmed:
        print(f"route passers not 4-hour confirmed: {', '.join(handoff.route_passers_not_confirmed)}")
    for warning in handoff.warnings:
        print(f"warning: {warning}")
    for plan in plans:
        print(f"wrote plan {plan}")
    print(f"plans written: {len(plans)}; no order placed")
    print(f"wrote {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
