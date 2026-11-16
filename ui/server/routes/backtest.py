"""Backtest: replay saved pipeline files, and rebuild the pipeline for past dates that have none.

GET /api/backtest reads files and cached bars and returns the results. GET /api/backtest/coverage says which dates
are saved, rebuilt or missing. POST /api/backtest/reconstruct starts the rebuild job (run_backtest_reconstruct),
which fetches missing daily bars and writes rebuilt reports under backtest/reconstructed. Nothing here places an order.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel

from extensions.backtest.engine import MAX_RANGE_DAYS, BacktestInputError, coverage, run_backtest
from ui.server import jobs, paths
from ui.server.routes import common

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


def _range(start: str, end: str | None) -> tuple[date, date]:
    first = date.fromisoformat(common.iso_date(start, "start"))
    last = date.fromisoformat(common.iso_date(end, "end")) if end else first
    if last < first:
        raise common.bad_request("end date is before start date")
    if (last - first).days >= MAX_RANGE_DAYS:
        raise common.bad_request(f"date range is limited to {MAX_RANGE_DAYS} days")
    return first, last


@router.get("")
def backtest(start: str, end: str | None = None, strategy: str = "all", timeframe: str = "daily",
             max_sessions: int = 10, risk_pct: float = 1.0, include_reconstructed: bool = True,
             hold_to_exit: bool = False) -> dict:
    first, last = _range(start, end)
    try:
        return run_backtest(paths.data_home(), first, last, strategy=strategy, timeframe=timeframe,
                            max_sessions=max_sessions, risk_pct=risk_pct, include_reconstructed=include_reconstructed,
                            hold_to_exit=hold_to_exit)
    except BacktestInputError as exc:
        raise common.bad_request(str(exc)) from None


@router.get("/coverage")
def backtest_coverage(start: str, end: str | None = None) -> list[dict]:
    first, last = _range(start, end)
    return coverage(paths.data_home(), first, last)


class Reconstruct(BaseModel):
    start: str
    end: str | None = None
    rebuild: bool = False  # rebuild dates that were already rebuilt
    all_dates: bool = False  # also rebuild dates that have saved files, to compare
    fetch_fundamentals: bool = False  # download missing SEC fundamentals the live way (slower)


@router.post("/reconstruct")
def reconstruct(body: Reconstruct) -> dict:
    first, last = _range(body.start, body.end)
    today = date.today()
    if last >= today:
        raise common.bad_request("the last date must be before today")
    args = ["--start", first.isoformat(), "--end", last.isoformat(), "--home", str(paths.data_home())]
    if body.rebuild:
        args.append("--rebuild")
    if body.all_dates:
        args.append("--all-dates")
    if body.fetch_fundamentals:
        args.append("--fetch-fundamentals")
    return common.start("backtest_reconstruct", [jobs.Step("run_backtest_reconstruct", args)], group="backtest")
