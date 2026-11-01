"""Validation shared by the routes. Every script argument is built from these, never from raw text."""

from __future__ import annotations

import re
from datetime import date

from fastapi import HTTPException

from ui.server import jobs, paths
from ui.server.queues import CATEGORIES

# The plain-US-ticker rule from extensions/market_data/alpaca_bars.py.
_TICKER = re.compile(r"^[A-Z]{1,5}(?:[.-][A-Z]{1,2})?$")
ANALYSTS = ("market", "social", "news", "fundamentals")
MAX_TICKERS = 50


def bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def iso_date(value: str | None, name: str = "as_of") -> str:
    try:
        return date.fromisoformat(value or "").isoformat()
    except ValueError:
        raise bad_request(f"{name} must be a date like 2026-09-30") from None


def ticker(value: str | None) -> str:
    cleaned = (value or "").strip().upper()
    if not _TICKER.fullmatch(cleaned):
        raise bad_request(f"not a valid US ticker: {value!r}")
    return cleaned


def tickers(values: list[str] | None) -> list[str]:
    cleaned = list(dict.fromkeys(ticker(v) for v in values or []))
    if len(cleaned) > MAX_TICKERS:
        raise bad_request(f"at most {MAX_TICKERS} tickers per run")
    return cleaned


def analysts(values: list[str] | None) -> list[str]:
    chosen = list(dict.fromkeys((v or "").strip().lower() for v in values or []))
    if not chosen or any(a not in ANALYSTS for a in chosen):
        raise bad_request(f"analysts must be chosen from {', '.join(ANALYSTS)}")
    return chosen


def start(kind: str, steps: list[jobs.Step], group: str | None = None) -> dict:
    try:
        job = jobs.runner.start(kind, steps, group)
    except jobs.JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return job.summary()


def input_steps(as_of: str) -> list[jobs.Step]:
    """The SCANBot inputs every scan reads, for a date that does not have them yet: the universe report
    (liquidity survivors and the asset snapshot behind them) and then the financials report on it. A date that
    already has a financials report needs nothing; one with a universe report but no financials needs only the
    financials step. Both steps read Alpaca and SEC EDGAR data only."""
    folder = paths.scanbot_dir()
    if any(folder.glob(f"financials_{as_of}_*.json")):
        return []
    steps = []
    if not any(folder.glob(f"universe_{as_of}_*.json")):
        steps.append(jobs.Step("run_scanbot_universe", ["--as-of", as_of, "--assets-dir", str(paths.data_home() / "assets"),
                                                        "--out-dir", str(folder)]))
    steps.append(jobs.Step("run_scanbot_financials", ["--as-of", as_of, "--report-dir", str(folder),
                                                      "--out-dir", str(folder)]))
    return steps


# Sideways channel scans read the newest financials report for the date (from
# run_scanbot_financials) and scan its survivors, so they take no ticker list.
CHANNEL_SCRIPTS = {"long_side": "run_scanbot_long_channel", "short_side": "run_scanbot_short_channel"}


def channel_scan_steps(as_of: str, which: tuple[str, ...] = ("long_side", "short_side")) -> list[jobs.Step]:
    folder = str(paths.scanbot_dir())
    return [
        jobs.Step(CHANNEL_SCRIPTS[w], ["--as-of", as_of, "--report-dir", folder, "--out-dir", folder])
        for w in which
    ]


def category(value: int) -> int:
    if value not in CATEGORIES:
        raise bad_request(f"category must be one of {', '.join(map(str, CATEGORIES))}")
    return value


# Category scans (run_category_scan) also read the newest financials report for the date, so they take no
# ticker list either. They write TickerBaskets to the UI's baskets folder, named apart from the trend scans.
CATEGORY_SCANS = ("up", "down", "side")


def category_scan_steps(as_of: str, which: tuple[str, ...], category: int) -> list[jobs.Step]:
    return [
        jobs.Step("run_category_scan", ["--scan", w, "--as-of", as_of, "--category", str(category),
                                        "--report-dir", str(paths.scanbot_dir()),
                                        "--out-dir", str(paths.baskets_dir())])
        for w in which
    ]


def regime_step(as_of: str, symbol: str) -> jobs.Step:
    return jobs.Step("run_regime_bot", ["--as-of", as_of, "--symbol", symbol,
                                        "--out-dir", str(paths.regimes_dir())])


def router_step(as_of: str, category: int = 1) -> jobs.Step:
    """The router reads the SCAN-UP / SCAN-DOWN baskets of `category`."""
    return jobs.Step("run_router", ["--as-of", as_of, "--category", str(category),
                                    "--basket-dir", str(paths.baskets_dir()),
                                    "--regime-dir", str(paths.regimes_dir()),
                                    "--out-dir", str(paths.routes_dir())])
