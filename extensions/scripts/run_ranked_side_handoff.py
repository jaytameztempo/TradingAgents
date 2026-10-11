"""Hand the ranked SIDE channel names to research under a SIDE market. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_ranked_side_handoff --as-of 2026-09-30

Reads the newest channel rank report in ~/.tradingagents/scanbot/ (or
--report-dir), the long and short channel reports it names, and the newest
SPY+QQQ route in ~/.tradingagents/routes/ (or --route-dir). The market label
must be SIDE. Every emitted long name and kept short name gets a plan in
~/.tradingagents/plans/, in rank order; dropped long names are NOT_RANKED with
no plan. A ranked name with no operator-supplied sector stops the run. The
handoff goes to ~/.tradingagents/handoffs/ unless --out-dir is set. Nothing
here calls Alpaca.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from extensions.research.ranked_side_handoff import ranked_side_handoff_for_date, save_ranked_side_handoff
from extensions.router.strategy_router import MissingInput

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hand the ranked SIDE channel names to research under a SIDE market (no orders)."
    )
    parser.add_argument("--as-of", required=True, help="date of the channel rank report and route, YYYY-MM-DD")
    parser.add_argument("--report-dir", help="folder of channel rank reports (default: ~/.tradingagents/scanbot)")
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
        handoff = ranked_side_handoff_for_date(as_of, args.report_dir, args.route_dir)
    except (MissingInput, ValueError) as exc:
        print(f"error: {exc}; no handoff written", file=sys.stderr)
        return EXIT_FAILED

    print(f"channel rank report: {handoff.rank_report}")
    print(f"route: {handoff.route_file}")
    print(f"market regime ({handoff.regime_symbol}): {handoff.regime_label}")
    path, plans = save_ranked_side_handoff(handoff, args.out_dir, args.plan_dir)
    for symbol, name in handoff.names.items():
        detail = name.get("reason") or f"score {name['score']}, {name['sector']}"
        print(f"  #{name['rank']:<3}{symbol:<6}{name['side']:<6}{name['status']:<11}{detail}")
    print(f"planned long ({len(handoff.planned_long)}): {' '.join(handoff.planned_long) or '-'}")
    print(f"planned short ({len(handoff.planned_short)}): {' '.join(handoff.planned_short) or '-'}")
    print(f"not ranked: {' '.join(handoff.not_ranked) or '-'}")
    for warning in handoff.warnings:
        print(f"warning: {warning}")
    print(f"plans written: {len(plans)}; no order placed")
    print(f"wrote {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
