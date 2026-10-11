"""Run the daily SIDE pipeline for one date, step by step. It never sends an order.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_daily --as-of 2026-10-09

Each step is the existing command, run in its own Python process, in this order:

     1. regime SPY            7. short channel
     2. regime QQQ            8. channel rank
     3. router                9. ranked side plans
     4. universe             10. 4-hour confirm
     5. financials           11. daily status
     6. long channel         12. earnings blackout
                             13. paper tickets

It stops at the first step that fails, and at two gates:

- after the router: if the SPY+QQQ market label is not SIDE, nothing after it runs.
- before channel rank: if any long or short channel member is missing from the
  operator sector map (extensions/scanbot/sector_map.py), it lists the missing
  names and stops. Nothing is ranked under an invented sector.

This command imports no Alpaca module and never imports paper_orders. Paper
tickets run in a fresh process, so their own refusal of Alpaca's trading
client still holds. Steps that fetch (regime, universe, financials, the
channel scans, 4-hour confirm) read market data only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from extensions.scanbot.sector_map import GICS_SECTOR
from extensions.status.daily_status import MARKET, SIDE, StatusInputError, default_report_dir, latest_route
from extensions.status.paper_tickets import TradingClientLoaded, refuse_trading_client

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2
REPO_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_MODULES = ("paper_orders", "alpaca.trading")


@dataclass(frozen=True)
class Step:
    name: str
    module: str  # run as python -m module
    argv: Callable[[str, argparse.Namespace], list[str]]


def _as_of(d: str, _args) -> list[str]:
    return ["--as-of", d]


def _cache(args) -> list[str]:
    return ["--cache-dir", args.cache_dir] if args.cache_dir else []


def _regime(symbol: str):
    return lambda d, args: ["--as-of", d, "--symbol", symbol, *_cache(args)]


STEPS_BEFORE_SIDE_GATE = [
    Step("regime SPY", "extensions.scripts.run_regime_bot", _regime("SPY")),
    Step("regime QQQ", "extensions.scripts.run_regime_bot", _regime("QQQ")),
    Step("router", "extensions.scripts.run_router", _as_of),
]
STEPS_BEFORE_SECTOR_GATE = [
    Step("universe", "extensions.scripts.run_scanbot_universe",
         lambda d, args: ["--as-of", d, "--preset", args.preset, *_cache(args)]),
    Step("financials", "extensions.scripts.run_scanbot_financials",
         lambda d, args: ["--as-of", d, *(["--offline"] if args.offline else [])]),
    Step("long channel", "extensions.scripts.run_scanbot_long_channel", lambda d, args: ["--as-of", d, *_cache(args)]),
    Step("short channel", "extensions.scripts.run_scanbot_short_channel", lambda d, args: ["--as-of", d, *_cache(args)]),
]
STEPS_AFTER_SECTOR_GATE = [
    Step("channel rank", "extensions.scripts.run_scanbot_channel_rank", _as_of),
    Step("ranked side plans", "extensions.scripts.run_ranked_side_handoff", _as_of),
    Step("4-hour confirm", "extensions.scripts.run_side_confirm_4h", lambda d, args: ["--as-of", d, *_cache(args)]),
    Step("daily status", "extensions.scripts.run_daily_status", _as_of),
    Step("earnings blackout", "extensions.scripts.run_earnings_blackout", _as_of),
    Step("paper tickets", "extensions.scripts.run_paper_tickets", _as_of),
]


def _run_step(step: Step, argv: list[str]) -> int:
    """Run one existing command in a fresh interpreter; its output goes straight to the console."""
    return subprocess.run([sys.executable, "-m", step.module, *argv], cwd=REPO_ROOT, check=False).returncode


def _assert_no_order_path() -> None:
    loaded = sorted(m for m in sys.modules if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_MODULES))
    if loaded:
        raise TradingClientLoaded(f"the daily command must not load {', '.join(loaded)}")


# --- gates -------------------------------------------------------------------

def market_label(as_of: date, route_dir: str | Path | None = None) -> tuple[Path, str]:
    """The SPY+QQQ market label on the newest route for the date."""
    path, route = latest_route(as_of, route_dir)
    return path, route["regime_label"]


def _newest_members(prefix: str, as_of: date, folder: Path) -> tuple[Path, list[str]]:
    found = []
    for path in sorted(folder.glob(f"{prefix}_{as_of}_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("as_of") == as_of.isoformat():
            found.append((datetime.fromisoformat(data["created_at"]), path, list(data["members"])))
    if not found:
        raise FileNotFoundError(f"no {prefix} report for {as_of} in {folder}")
    _, path, members = max(found, key=lambda item: item[0])
    return path, members


def missing_sectors(as_of: date, report_dir: str | Path | None = None) -> list[str]:
    """Long and short channel members for the date that the operator sector map does not cover."""
    folder = Path(report_dir) if report_dir is not None else default_report_dir()
    missing = []
    for prefix in ("long_channel", "short_channel"):
        _, members = _newest_members(prefix, as_of, folder)
        missing += [s for s in members if s not in GICS_SECTOR and s not in missing]
    return missing


# --- the run -----------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the daily SIDE pipeline for one date (never sends an order).")
    parser.add_argument("--as-of", required=True, help="the date to run, YYYY-MM-DD")
    parser.add_argument("--cache-dir", help="reuse bars for completed ranges from this folder (passed to bar steps)")
    parser.add_argument("--offline", action="store_true", help="financials use cached fundamentals only")
    parser.add_argument("--preset", default="DEFAULT", help="universe liquidity preset (default: DEFAULT)")
    return parser


def _steps(steps: list[Step], d: str, args, done: list[tuple[str, str]]) -> bool:
    for step in steps:
        print(f"\n=== {step.name} ===", flush=True)
        code = _run_step(step, step.argv(d, args))
        if code != EXIT_OK:
            done.append((step.name, f"FAILED (exit {code})"))
            return False
        done.append((step.name, "ok"))
    return True


def _summary(as_of: date, done: list[tuple[str, str]], outcome: str) -> None:
    print(f"\ndaily run {as_of}: {outcome}")
    for name, result in done:
        print(f"  {name:<20}{result}")
    print("  no order sent")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print(f"error: --as-of {args.as_of!r} is not YYYY-MM-DD", file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        _assert_no_order_path()
        refuse_trading_client()
    except TradingClientLoaded as exc:
        print(f"error: {exc}; nothing run", file=sys.stderr)
        return EXIT_BAD_INPUT
    d = as_of.isoformat()
    done: list[tuple[str, str]] = []

    if not _steps(STEPS_BEFORE_SIDE_GATE, d, args, done):
        _summary(as_of, done, "stopped at a failed step")
        return EXIT_FAILED
    try:
        route_path, label = market_label(as_of)
    except StatusInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        done.append(("SIDE gate", "FAILED (no usable route)"))
        _summary(as_of, done, "stopped: no usable route")
        return EXIT_FAILED
    if label != SIDE:
        done.append(("SIDE gate", f"STOP: market label {label}"))
        _summary(as_of, done, f"stopped: {MARKET} market label is {label}, not SIDE ({route_path.name})")
        return EXIT_OK
    done.append(("SIDE gate", "SIDE"))

    if not _steps(STEPS_BEFORE_SECTOR_GATE, d, args, done):
        _summary(as_of, done, "stopped at a failed step")
        return EXIT_FAILED
    try:
        missing = missing_sectors(as_of)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: cannot read the channel reports: {exc!r}", file=sys.stderr)
        done.append(("sector gate", "FAILED (unreadable channel report)"))
        _summary(as_of, done, "stopped: cannot check sectors")
        return EXIT_FAILED
    if missing:
        done.append(("sector gate", f"STOP: {len(missing)} missing"))
        _summary(as_of, done, "stopped: names missing from the sector map; nothing ranked")
        print("missing from extensions/scanbot/sector_map.py (add each with its GICS sector, then rerun):")
        for symbol in missing:
            print(f"  {symbol}")
        return EXIT_FAILED
    done.append(("sector gate", "all mapped"))

    if not _steps(STEPS_AFTER_SECTOR_GATE, d, args, done):
        _summary(as_of, done, "stopped at a failed step")
        return EXIT_FAILED
    _assert_no_order_path()
    _summary(as_of, done, "complete")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
