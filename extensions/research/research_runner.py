"""Run TradingAgents on one routed ticker for research, and save its report.

The latest route decision for the date says whether UP playbooks are ALLOWED or
BLOCKED for the ticker. Either way the run is research only: TradingAgents'
rating is printed and its report saved, and no order is placed. TradingAgents
gets its data from its own vendors; nothing here calls Alpaca.

A ticker that is not in the route is refused, and a missing route stops the run.
"""

from __future__ import annotations

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

ALLOWED, BLOCKED = "ALLOWED", "BLOCKED"
# The analyst keys TradingAgentsGraph accepts.
ANALYSTS = ("market", "social", "news", "fundamentals")
DEFAULT_ANALYSTS = ("market",)


class TickerNotInRoute(ValueError):
    """The ticker is neither tradeable nor blocked in the route, so it was never routed."""


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
    status = route_status(decision, ticker)
    ticker = validate_ticker(ticker)
    graph = (graph_factory or make_graph)(validate_analysts(analysts))
    final_state, rating = graph.propagate(ticker, decision.as_of.isoformat())
    report_path = graph.save_reports(final_state, ticker)
    return ResearchResult(ticker, decision.as_of, status, str(rating), Path(report_path))
