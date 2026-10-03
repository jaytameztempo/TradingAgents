"""Research: run TradingAgents on a routed ticker (via run_research) and read its reports."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ui.server import files, jobs, paths
from ui.server.routes import common

router = APIRouter(prefix="/api/research", tags=["research"])


class ResearchRun(BaseModel):
    as_of: str
    ticker: str
    analysts: list[str] = ["market"]
    # The page asks before spending on LLM calls; a request without it is refused.
    confirm: bool = False


@router.get("/runs")
def runs() -> list[dict]:
    return files.list_state_logs(paths.results_dir())


@router.get("/state/{ticker}/{date}")
def state(ticker: str, date: str) -> dict:
    try:
        return files.read_state_log(paths.results_dir(), ticker, date)
    except files.NotFound:
        raise HTTPException(status_code=404, detail="no such research run") from None


@router.get("/reports")
def reports() -> list[dict]:
    return files.list_report_dirs(paths.results_dir())


@router.get("/reports/{name}")
def report(name: str) -> dict:
    try:
        return files.read_report_dir(paths.results_dir(), name)
    except files.NotFound:
        raise HTTPException(status_code=404, detail="no such report") from None


@router.post("/run")
def run(body: ResearchRun) -> dict:
    if not body.confirm:
        raise common.bad_request("research runs call the LLM; confirm the run first")
    args = ["--as-of", common.iso_date(body.as_of), "--ticker", common.ticker(body.ticker),
            "--analysts", *common.analysts(body.analysts), "--route-dir", str(paths.routes_dir())]
    return common.start("research", [jobs.Step("run_research", args)], group="research")
