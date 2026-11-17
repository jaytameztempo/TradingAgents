"""Backtest: replay saved pipeline files, and rebuild the pipeline for past dates that have none.

GET /api/backtest reads files and cached bars and returns the results. GET /api/backtest/coverage says which dates
are saved, rebuilt or missing. POST /api/backtest/reconstruct starts the rebuild job (run_backtest_reconstruct),
which fetches missing daily bars and writes rebuilt reports under backtest/reconstructed. /api/backtest/runs saves a
backtest result under backtest/runs, lists, opens, renames, exports (JSON or CSV) and deletes saved runs. Nothing here
places an order.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from extensions.backtest import runs
from extensions.backtest import triggers as tb
from extensions.backtest.engine import MAX_RANGE_DAYS, BacktestInputError, coverage, run_backtest
from extensions.status.paper_tickets import TicketInputError, load_series
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


class Settings(BaseModel):
    """The backtest settings (GET /api/backtest takes them as query parameters, POST /runs as JSON)."""
    start: str
    end: str | None = None
    strategy: str = "all"
    timeframe: str = "daily"
    max_sessions: int = 10
    risk_pct: float = 1.0
    include_reconstructed: bool = True
    hold_to_exit: bool = False
    capital: float = 100_000.0
    origins: str | None = None  # both, replay or reconstructed (default from include_reconstructed)
    sizing: str = "live"  # live or fixed
    max_positions: int | None = None  # None: the sizing mode's default (live: the guard's)
    max_gross_pct: float | None = None
    max_open_risk_pct: float | None = None
    slippage_bps: float = 0.0
    commission_per_share: float = 0.0
    buying_power: float | None = None  # None: the starting capital
    entry_mode: str = "next_day"  # SIDE: tradebot (queue, confirm, trigger) or next_day
    squeeze_release: bool | None = None  # the Tradebot filter switches; None: the Tradebot settings
    rsi_extreme: bool | None = None
    light_volume: bool | None = None
    squeeze_4h1h: bool | None = None
    expire_sessions: int | None = None  # None: the Tradebot settings


@router.get("")
def backtest(start: str, end: str | None = None, strategy: str = "all", timeframe: str = "daily",
             max_sessions: int = 10, risk_pct: float = 1.0, include_reconstructed: bool = True,
             hold_to_exit: bool = False, capital: float = 100_000.0, origins: str | None = None,
             sizing: str = "live", max_positions: int | None = None, max_gross_pct: float | None = None,
             max_open_risk_pct: float | None = None, slippage_bps: float = 0.0,
             commission_per_share: float = 0.0, buying_power: float | None = None, entry_mode: str = "next_day",
             squeeze_release: bool | None = None, rsi_extreme: bool | None = None, light_volume: bool | None = None,
             squeeze_4h1h: bool | None = None, expire_sessions: int | None = None) -> dict:
    return _run(Settings(**{k: v for k, v in locals().items()}))


def _run(q: Settings) -> dict:
    first, last = _range(q.start, q.end)
    settings_filters, settings_expire = tb.settings_defaults()
    filters = {k: (settings_filters[k] if getattr(q, k) is None else getattr(q, k)) for k in tb.FILTERS}
    expire = settings_expire if q.expire_sessions is None else q.expire_sessions
    try:
        return run_backtest(paths.data_home(), first, last, strategy=q.strategy, timeframe=q.timeframe,
                            max_sessions=q.max_sessions, risk_pct=q.risk_pct,
                            include_reconstructed=q.include_reconstructed, hold_to_exit=q.hold_to_exit,
                            capital=q.capital, origins=q.origins, sizing_mode=q.sizing,
                            max_positions=q.max_positions, max_gross_pct=q.max_gross_pct,
                            max_open_risk_pct=q.max_open_risk_pct, slippage_bps=q.slippage_bps,
                            commission_per_share=q.commission_per_share, buying_power=q.buying_power,
                            entry_mode=q.entry_mode, filters=filters, expire_sessions=expire)
    except BacktestInputError as exc:
        raise common.bad_request(str(exc)) from None


@router.get("/defaults")
def backtest_defaults() -> dict:
    """The live series' original buying power (tickets/series.json), the Buying power field's default."""
    try:
        series = load_series(paths.data_home() / "tickets" / "series.json")
    except TicketInputError:
        series = None
    bp = (series or {}).get("original_buying_power")
    filters, expire = tb.settings_defaults()
    return {"series_original_buying_power": float(bp) if isinstance(bp, int | float) else None,
            "series_recorded_at": (series or {}).get("recorded_at"),
            "tradebot_filters": filters, "tradebot_expire_sessions": expire}


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
    trade_bars: bool = False  # also fetch the daily and 4-hour bars of the names a backtest would trade


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
    if body.trade_bars:
        args.append("--trade-bars")
    return common.start("backtest_reconstruct", [jobs.Step("run_backtest_reconstruct", args)], group="backtest")


class SaveRun(Settings):
    """The backtest settings to run and save (the same as GET /api/backtest), and an optional name."""
    name: str = Field("", max_length=runs.MAX_NAME)


class Rename(BaseModel):
    name: str = Field(..., min_length=1, max_length=runs.MAX_NAME)


@router.get("/runs")
def list_runs() -> list[dict]:
    return runs.list_runs(paths.data_home())


@router.post("/runs")
def save_run(body: SaveRun) -> dict:
    result = _run(Settings(**body.model_dump(exclude={"name"})))
    return runs.save_run(paths.data_home(), result, body.name)


def _load(run_id: str) -> dict:
    try:
        return runs.load_run(paths.data_home(), run_id)
    except runs.RunNotFound:
        raise HTTPException(status_code=404, detail="no such saved run") from None


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    return _load(run_id)


@router.put("/runs/{run_id}")
def rename_run(run_id: str, body: Rename) -> dict:
    _load(run_id)
    return runs.rename_run(paths.data_home(), run_id, body.name)


@router.get("/runs/{run_id}/export")
def export_run(run_id: str, format: str = "json") -> Response:
    run = _load(run_id)
    try:
        body, media, name = runs.export_run(run, format)
    except ValueError as exc:
        raise common.bad_request(str(exc)) from None
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.delete("/runs/{run_id}")
def delete_run(run_id: str) -> dict:
    _load(run_id)
    runs.delete_run(paths.data_home(), run_id)
    return {"deleted": run_id}
