"""4-hour confirmation of the planned names in a ranked side handoff. It places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_side_confirm_4h --as-of 2026-09-30
    python -m extensions.scripts.run_side_confirm_4h --handoff path/to/ranked_side_handoff_....json

--as-of picks the newest ranked side handoff for that date in --handoff-dir.
Only the planned long and short names are checked. A long passes if the last
4-hour close is within 0.6 ATR above support and no more than 0.25 ATR through
it; a short is the mirror at resistance. Bars are regular-session 4-hour bars
built from SIP 30-minute bars dated on or before the as-of date, with no
extended hours and no hourly bars. If Alpaca refuses them, nothing is written.
Plans are not rewritten. The report goes to ~/.tradingagents/scanbot/ unless
--out-dir is set. Keys are read from ALPACA_API_KEY and ALPACA_SECRET_KEY, in
the environment or in .env.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from extensions.market_data.alpaca_bars import MissingAlpacaKeys
from extensions.router.strategy_router import MissingInput
from extensions.scanbot.funnel import DATA_ERRORS, CalendarError
from extensions.scanbot.side_confirm_4h import (
    REASONS,
    SIDES,
    latest_ranked_side_handoff,
    run_side_confirm_4h,
    save_report,
)

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2
READ_ERRORS = (OSError, ValueError, KeyError, TypeError)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="4-hour confirmation of planned names in a ranked side handoff (no orders)."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--handoff", help="the ranked side handoff JSON whose planned names to check")
    source.add_argument("--as-of", help="use the newest ranked side handoff for this date, YYYY-MM-DD")
    parser.add_argument("--handoff-dir", help="where to look for handoffs (default: ~/.tradingagents/handoffs)")
    parser.add_argument("--cache-dir", help="reuse bars for completed ranges from this folder")
    parser.add_argument("--out-dir", help="folder for the report JSON (default: ~/.tradingagents/scanbot)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.handoff:
            path = Path(args.handoff)
            handoff = json.loads(path.read_text(encoding="utf-8"))
        else:
            try:
                as_of = date.fromisoformat(args.as_of)
            except ValueError:
                print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
                return EXIT_BAD_INPUT
            path, handoff = latest_ranked_side_handoff(as_of, args.handoff_dir)
    except (MissingInput, *READ_ERRORS) as exc:
        print(f"error: cannot read ranked side handoff: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    load_dotenv(find_dotenv(usecwd=True))
    print(f"ranked side handoff {path}: planned {len(handoff.get('planned_long', []))} long, "
          f"{len(handoff.get('planned_short', []))} short as of {handoff.get('as_of')}")
    try:
        report = run_side_confirm_4h(handoff, path, cache_dir=args.cache_dir)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except (MissingAlpacaKeys, CalendarError, *DATA_ERRORS) as exc:
        print(f"error: {exc}; no report written", file=sys.stderr)
        return EXIT_FAILED

    print(f"side 4-hour confirmation as of {report.as_of}  (regular session 4h from 30m, feed {report.policy['feed']}; "
          f"near <= {report.policy['near_atr_max']:g} ATR, broken > {report.policy['break_atr_max']:g} ATR)")
    for side in SIDES:
        c = report.counts[side]
        print(f"  {side}: checked {c['checked_4h']} of {c['planned']} planned, pass {c['pass_4h']}, fail {c['fail_4h']}")
        print("    fail reasons (a name can have several):")
        for code in REASONS:
            print(f"      {code:<36} {c['reason_counts'][code]:>4}")
        for symbol, call in report.calls[side].items():
            print(f"    {symbol}: {'PASS' if call['confirm_pass'] else 'FAIL'}  {'; '.join(call['detail'])}")
        print(f"    CONFIRMED: {' '.join(report.confirmed[side]) or '-'}")
    t = report.counts["total"]
    print(f"  total: checked {t['checked_4h']}, pass {t['pass_4h']}, fail {t['fail_4h']}; plans not rewritten; "
          "no order placed")
    for warning in report.warnings:
        print(f"warning: {warning}")

    out = save_report(report, args.out_dir)
    print(f"wrote {out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
