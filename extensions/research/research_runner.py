"""Run TradingAgents on one routed ticker for research, and save its report.

The latest route decision for the date says whether UP playbooks are ALLOWED or
BLOCKED for the ticker. Either way the run is research only: TradingAgents'
rating is printed and its report saved, and no order is placed. TradingAgents
gets its data from its own vendors; nothing here calls Alpaca.

A ticker that is not in the route is refused, and a missing route stops the run.

The route lists only the UP and DOWN scan passers, so a sideways scan's names are never in it.
Those are researched from their scan file instead: a passing member of a scan for the same date
gets the status SCANNED, and the route is not read.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from extensions.market_data.alpaca_bars import validate_ticker
from extensions.router.strategy_router import (
    MissingInput,
    RouteDecision,
    default_route_dir,
    load_decision,
)

ALLOWED, BLOCKED, SCANNED = "ALLOWED", "BLOCKED", "SCANNED"
# The analyst keys TradingAgentsGraph accepts.
ANALYSTS = ("market", "social", "news", "fundamentals")
DEFAULT_ANALYSTS = ("market",)


class TickerNotInRoute(ValueError):
    """The ticker is neither tradeable nor blocked in the route, so it was never routed."""


class TickerNotInScan(ValueError):
    """The scan file is for another date, or the ticker is not one of its passing members."""


@dataclass(frozen=True)
class ResearchResult:
    ticker: str
    as_of: date
    status: str
    rating: str
    report_path: Path


def latest_route(as_of: date, route_dir: str | Path | None = None) -> tuple[Path, RouteDecision]:
    """The newest route decision for as_of, by the created_at inside each file."""
    folder = Path(route_dir) if route_dir is not None else default_route_dir()
    found = []
    for path in sorted(folder.glob(f"*_{as_of}_*.json")):
        try:
            decision = load_decision(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MissingInput(f"cannot read route file {path}: {exc}") from exc
        if decision.as_of == as_of:
            found.append((path, decision))
    if not found:
        raise MissingInput(f"no route decision for {as_of} in {folder}")
    return max(found, key=lambda item: item[1].created_at)


def route_status(decision: RouteDecision, ticker: str) -> str:
    """ALLOWED if the route lists the ticker as tradeable, BLOCKED if it lists it as blocked."""
    ticker = validate_ticker(ticker)
    if ticker in decision.tradeable:
        return ALLOWED
    if ticker in decision.blocked:
        return BLOCKED
    routed = ", ".join(decision.tradeable + decision.blocked) or "none"
    raise TickerNotInRoute(f"{ticker} is not in the route for {decision.as_of} (routed tickers: {routed})")


def scan_status(scan_file: str | Path, ticker: str, as_of: date) -> str:
    """SCANNED if the scan file is for as_of and lists the ticker as a passing member.

    TickerBasket members are objects with a ticker; channel report members are plain tickers.
    """
    path = Path(scan_file)
    ticker = validate_ticker(ticker)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MissingInput(f"cannot read scan file {path}: {exc}") from exc
    if data.get("as_of") != as_of.isoformat():
        raise TickerNotInScan(f"{path.name} is for {data.get('as_of')}, not {as_of}")
    members = [m.get("ticker") if isinstance(m, dict) else m for m in data.get("members") or []]
    if ticker not in members:
        raise TickerNotInScan(f"{ticker} is not a passing member of {path.name} (members: {', '.join(members) or 'none'})")
    return SCANNED


def validate_analysts(analysts: Iterable[str]) -> tuple[str, ...]:
    chosen = tuple(dict.fromkeys(a.strip().lower() for a in analysts))
    unknown = [a for a in chosen if a not in ANALYSTS]
    if not chosen or unknown:
        raise ValueError(f"analysts must be chosen from {', '.join(ANALYSTS)}; got {', '.join(chosen) or 'none'}")
    return chosen


def make_graph(analysts: tuple[str, ...]):
    """A TradingAgentsGraph with the default config, which already applies .env overrides."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    return TradingAgentsGraph(selected_analysts=analysts, config=DEFAULT_CONFIG.copy())


def run_research(
    decision: RouteDecision,
    ticker: str,
    analysts: Iterable[str] = DEFAULT_ANALYSTS,
    graph_factory: Callable[[tuple[str, ...]], object] | None = None,
) -> ResearchResult:
    """Run TradingAgents on a routed ticker and save its report. Places no order."""
    return _run(validate_ticker(ticker), decision.as_of, route_status(decision, ticker), analysts, graph_factory)


def run_scan_research(
    scan_file: str | Path,
    as_of: date,
    ticker: str,
    analysts: Iterable[str] = DEFAULT_ANALYSTS,
    graph_factory: Callable[[tuple[str, ...]], object] | None = None,
) -> ResearchResult:
    """Run TradingAgents on a passing member of a scan file and save its report. Places no order."""
    return _run(validate_ticker(ticker), as_of, scan_status(scan_file, ticker, as_of), analysts, graph_factory)


def _run(ticker, as_of, status, analysts, graph_factory) -> ResearchResult:
    graph = (graph_factory or make_graph)(validate_analysts(analysts))
    final_state, rating = graph.propagate(ticker, as_of.isoformat())
    report_path = graph.save_reports(final_state, ticker)
    return ResearchResult(ticker, as_of, status, str(rating), Path(report_path))
