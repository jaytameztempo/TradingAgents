"""Dashboard, Scans and Strategy: baskets, regime labels, routes and playbook plans."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ui.server import files, jobs, paths
from ui.server.routes import common

router = APIRouter(prefix="/api", tags=["pipeline"])

SCAN_PREFIXES = {"up": "scan-upward-trend-momentum", "down": "scan-breakdown-short-candidates"}
CHANNEL_PREFIXES = {"long": "long_channel", "short": "short_channel"}
# Scan picker on the Scans page -> (trend scans, sideways channel scans).
SCAN_KINDS = {
    "up": (("up",), ()),
    "down": (("down",), ()),
    "both": (("up", "down"), ()),
    "long_side": ((), ("long_side",)),
    "short_side": ((), ("short_side",)),
    "sideways": ((), ("long_side", "short_side")),
    "all": (("up", "down"), ("long_side", "short_side")),
}
BOT_SCRIPTS = {"up": "run_up_bot", "down": "run_down_bot", "side": "run_side_bot"}


def _file(folder, name: str) -> dict:
    try:
        return files.read_json_file(files.safe_child(folder, name))
    except files.NotFound:
        raise HTTPException(status_code=404, detail="no such file") from None


# --- reads ---------------------------------------------------------------------------

@router.get("/dates")
def dates() -> list[str]:
    """Every as_of date with a basket, label or route, newest first."""
    return files.artifact_dates(paths.baskets_dir(), paths.regimes_dir(), paths.routes_dir())


@router.get("/summary")
def summary(as_of: str | None = None) -> dict:
    """The newest regime, baskets, route and plans for a date (default: the newest date on disk)."""
    as_of = common.iso_date(as_of) if as_of else next(iter(dates()), None)
    if as_of is None:
        return {"as_of": None, "regime": None, "baskets": {}, "route": None, "plans": []}
    return {
        "as_of": as_of,
        "regime": files.latest(paths.regimes_dir(), as_of),
        "baskets": {k: files.latest(paths.baskets_dir(), as_of, p) for k, p in SCAN_PREFIXES.items()},
        "route": files.latest(paths.routes_dir(), as_of),
        "plans": [p for p in files.list_plans(paths.plans_dir()) if p["as_of"] == as_of],
    }


@router.get("/baskets")
def baskets(kind: str | None = None, as_of: str | None = None) -> list[dict]:
    if kind is not None and kind not in SCAN_PREFIXES:
        raise common.bad_request("kind must be up or down")
    prefix = SCAN_PREFIXES[kind] if kind else None
    return files.list_artifacts(paths.baskets_dir(), prefix, common.iso_date(as_of) if as_of else None)


@router.get("/baskets/{name}")
def basket(name: str) -> dict:
    return _file(paths.baskets_dir(), name)


def _channel_summary(entry: dict) -> dict:
    """A channel report without its per-ticker calls (hundreds of names) except the members'."""
    data = entry.get("data")
    if not isinstance(data, dict):
        return entry
    calls = data.get("calls") or {}
    members = data.get("members") or []
    slim = {k: v for k, v in data.items() if k != "calls"}
    slim["member_calls"] = {m: calls.get(m) for m in members}
    slim["scanned"] = len(calls)
    return {**entry, "data": slim}


@router.get("/channel-scans")
def channel_scans(side: str) -> list[dict]:
    """Sideways channel reports (long or short), newest first, members' calls only."""
    prefix = CHANNEL_PREFIXES.get(side)
    if prefix is None:
        raise common.bad_request("side must be long or short")
    return [_channel_summary(e) for e in files.list_artifacts(paths.scanbot_dir(), prefix)]


@router.get("/regimes")
def regimes() -> list[dict]:
    return files.list_artifacts(paths.regimes_dir())


@router.get("/routes")
def routes() -> list[dict]:
    return files.list_artifacts(paths.routes_dir())


@router.get("/plans")
def plans() -> list[dict]:
    return files.list_plans(paths.plans_dir())


# --- runs ----------------------------------------------------------------------------

class ScanRun(BaseModel):
    as_of: str
    kind: str = "both"  # a key of SCAN_KINDS
    tickers: list[str] = []


class PipelineRun(BaseModel):
    as_of: str
    tickers: list[str] = []
    regime_symbol: str = "SPY"


class RegimeRun(BaseModel):
    as_of: str
    symbol: str = "SPY"


class RouterRun(BaseModel):
    as_of: str


class BotRun(BaseModel):
    as_of: str
    bot: str  # up, down or side
    symbol: str


@router.post("/scans/run")
def run_scans(body: ScanRun) -> dict:
    kinds = SCAN_KINDS.get(body.kind)
    if kinds is None:
        raise common.bad_request(f"kind must be one of {', '.join(SCAN_KINDS)}")
    trend, channel = kinds
    as_of, universe = common.iso_date(body.as_of), common.tickers(body.tickers)
    steps = [*(common.scan_steps(as_of, universe, trend) if trend else []),
             *(common.channel_scan_steps(as_of, channel) if channel else [])]
    return common.start(f"scan:{body.kind}", steps)


@router.post("/pipeline/run")
def run_pipeline(body: PipelineRun) -> dict:
    """Both scans, then RegimeBot, then the router, stopping at the first failure."""
    as_of = common.iso_date(body.as_of)
    steps = [
        *common.scan_steps(as_of, common.tickers(body.tickers)),
        common.regime_step(as_of, common.ticker(body.regime_symbol)),
        common.router_step(as_of),
    ]
    return common.start("pipeline", steps, group="pipeline")


@router.post("/regime/run")
def run_regime(body: RegimeRun) -> dict:
    return common.start("regime", [common.regime_step(common.iso_date(body.as_of), common.ticker(body.symbol))])


@router.post("/router/run")
def run_router(body: RouterRun) -> dict:
    return common.start("router", [common.router_step(common.iso_date(body.as_of))])


@router.post("/bots/run")
def run_bot(body: BotRun) -> dict:
    script = BOT_SCRIPTS.get(body.bot)
    if script is None:
        raise common.bad_request("bot must be up, down or side")
    args = ["--as-of", common.iso_date(body.as_of), "--symbol", common.ticker(body.symbol),
            "--route-dir", str(paths.routes_dir()), "--out-dir", str(paths.plans_dir())]
    return common.start(f"bot:{body.bot}", [jobs.Step(script, args)])
