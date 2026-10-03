"""Validation shared by the routes. Every script argument is built from these, never from raw text."""

from __future__ import annotations

import re
from datetime import date

from fastapi import HTTPException

from ui.server import jobs, paths

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


def scan_steps(as_of: str, universe: list[str], which: tuple[str, ...] = ("up", "down")) -> list[jobs.Step]:
    """Steps for the upward and/or breakdown scans, writing to the UI's baskets folder."""
    scripts = {"up": "run_upward_trend_scan", "down": "run_breakdown_scan"}
    extra = ["--tickers", *universe] if universe else []
    return [
        jobs.Step(scripts[w], ["--as-of", as_of, *extra, "--out-dir", str(paths.baskets_dir())])
        for w in which
    ]


def regime_step(as_of: str, symbol: str) -> jobs.Step:
    return jobs.Step("run_regime_bot", ["--as-of", as_of, "--symbol", symbol,
                                        "--out-dir", str(paths.regimes_dir())])


def router_step(as_of: str) -> jobs.Step:
    return jobs.Step("run_router", ["--as-of", as_of, "--basket-dir", str(paths.baskets_dir()),
                                    "--regime-dir", str(paths.regimes_dir()),
                                    "--out-dir", str(paths.routes_dir())])
