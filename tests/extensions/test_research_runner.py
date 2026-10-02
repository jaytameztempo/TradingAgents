"""The research runner prints ALLOWED or BLOCKED, runs TradingAgents, saves the report, and never orders.

A stand-in graph replaces TradingAgents, so no test calls an LLM; Alpaca is made to fail if touched.
"""

import ast
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alpaca.data.historical import StockHistoricalDataClient

from extensions.regime import regime_bot
from extensions.research import research_runner
from extensions.research.research_runner import (
    ALLOWED,
    ANALYSTS,
    BLOCKED,
    TickerNotInRoute,
    latest_route,
    make_graph,
    route_status,
    run_research,
)
from extensions.router.strategy_router import MissingInput, RouteDecision, save_decision
from extensions.scans import upward_trend_momentum
from extensions.scripts import run_research as cli

AS_OF = date(2026, 9, 30)
T0 = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)


class FakeGraph:
    """Stands in for TradingAgentsGraph: records the run and 'saves' a report path."""

    instances = []

    def __init__(self, analysts, report_root=Path("reports")):
        self.analysts = analysts
        self.report_root = report_root
        self.propagated = []
        self.saved = []
        FakeGraph.instances.append(self)

    def propagate(self, ticker, trade_date):
        self.propagated.append((ticker, trade_date))
        return {"final_trade_decision": "research"}, "Overweight"

    def save_reports(self, final_state, ticker):
        path = self.report_root / f"{ticker}_report"
        self.saved.append((final_state, ticker))
        return path


@pytest.fixture(autouse=True)
def _no_alpaca(monkeypatch):
    """Nothing in the research path may build an Alpaca client or fetch bars."""
    def refuse(*args, **kwargs):
        pytest.fail("touched Alpaca")

    monkeypatch.setattr(StockHistoricalDataClient, "__init__", refuse)
    for module in (upward_trend_momentum, regime_bot):
        monkeypatch.setattr(module, "fetch_daily_bars", refuse)
    FakeGraph.instances.clear()


@pytest.fixture
def fake_graph(monkeypatch, tmp_path):
    monkeypatch.setattr(research_runner, "make_graph", lambda analysts: FakeGraph(analysts, tmp_path))


def _decision(up_allowed=False, tickers=("NVDA", "SPY"), as_of=AS_OF, created_at=T0):
    tickers = list(tickers)
    return RouteDecision(
        as_of=as_of,
        created_at=created_at,
        series="UP",
        regime_symbol="SPY",
        regime_label="UP" if up_allowed else "SIDE",
        up_allowed=up_allowed,
        tradeable=tickers if up_allowed else [],
        blocked=[] if up_allowed else tickers,
        reason="SPY regime is UP: allowed" if up_allowed else "SPY regime is SIDE: UP playbooks are blocked",
        basket_file="b.json",
        regime_file="r.json",
    )


@pytest.mark.unit
def test_analyst_keys_match_tradingagents():
    from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS

    assert set(ANALYSTS) == set(ANALYST_NODE_SPECS)


@pytest.mark.unit
def test_status_comes_from_the_route():
    assert route_status(_decision(up_allowed=True), "nvda") == ALLOWED
    assert route_status(_decision(up_allowed=False), "NVDA") == BLOCKED


@pytest.mark.unit
def test_a_ticker_outside_the_route_is_refused():
    with pytest.raises(TickerNotInRoute, match="AAPL is not in the route"):
        route_status(_decision(), "AAPL")


@pytest.mark.unit
@pytest.mark.parametrize("up_allowed, status", [(True, ALLOWED), (False, BLOCKED)])
def test_research_runs_and_saves_a_report_either_way(up_allowed, status, tmp_path):
    graphs = []

    def factory(analysts):
        graphs.append(FakeGraph(analysts, tmp_path))
        return graphs[-1]

    result = run_research(_decision(up_allowed), "NVDA", graph_factory=factory)

    (graph,) = graphs
    assert graph.analysts == ("market",)
    assert graph.propagated == [("NVDA", "2026-09-30")]
    assert graph.saved == [({"final_trade_decision": "research"}, "NVDA")]
    assert result.status == status
    assert result.rating == "Overweight"
    assert result.report_path == tmp_path / "NVDA_report"


@pytest.mark.unit
def test_a_ticker_outside_the_route_never_builds_a_graph():
    with pytest.raises(TickerNotInRoute):
        run_research(_decision(), "AAPL", graph_factory=lambda a: pytest.fail("built a graph"))


@pytest.mark.unit
def test_make_graph_passes_the_analysts_and_default_config(monkeypatch):
    from tradingagents.graph import trading_graph

    built = {}

    class Recorder:
        def __init__(self, selected_analysts, config):
            built.update(analysts=selected_analysts, config=config)

    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", Recorder)

    make_graph(("market",))

    assert built["analysts"] == ("market",)
    assert "llm_provider" in built["config"]


@pytest.mark.unit
def test_the_newest_route_for_the_date_wins(tmp_path):
    save_decision(_decision(up_allowed=False, created_at=T0), tmp_path)
    save_decision(_decision(up_allowed=True, created_at=T0 + timedelta(hours=1)), tmp_path)
    save_decision(_decision(up_allowed=False, created_at=T0 + timedelta(hours=2), as_of=AS_OF - timedelta(days=1)), tmp_path)

    _, decision = latest_route(AS_OF, tmp_path)

    assert decision.up_allowed


@pytest.mark.unit
def test_a_missing_route_stops(tmp_path):
    with pytest.raises(MissingInput, match="no route decision for 2026-09-30"):
        latest_route(AS_OF, tmp_path)


@pytest.mark.unit
def test_an_unreadable_route_file_stops(tmp_path):
    save_decision(_decision(), tmp_path)
    (tmp_path / "route_UP_2026-09-30_20260930T230000Z.json").write_text("{", encoding="utf-8")
    with pytest.raises(MissingInput, match="cannot read route file"):
        latest_route(AS_OF, tmp_path)


@pytest.mark.unit
def test_the_research_code_never_names_alpaca_or_orders():
    """The runner reaches TradingAgents only; it has no broker path at all."""
    for module in (research_runner, cli):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not any(m.startswith("alpaca") for m in imported), module.__name__
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not {n for n in names if "order" in n.lower()}, module.__name__


def _argv(route_dir, ticker="NVDA", *extra):
    return ["--as-of", "2026-09-30", "--ticker", ticker, "--route-dir", str(route_dir), *extra]


@pytest.mark.unit
def test_cli_prints_blocked_then_runs_and_saves(tmp_path, fake_graph, capsys):
    routes = tmp_path / "routes"
    save_decision(_decision(up_allowed=False), routes)

    assert cli.main(_argv(routes)) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert out.startswith("BLOCKED: NVDA as of 2026-09-30")
    assert out.index("BLOCKED") < out.index("Running TradingAgents")
    assert "rating for NVDA: Overweight (research only; no order placed)" in out
    assert f"report saved to {tmp_path / 'NVDA_report'}" in out
    (graph,) = FakeGraph.instances
    assert graph.propagated == [("NVDA", "2026-09-30")]


@pytest.mark.unit
def test_cli_prints_allowed_then_runs_without_ordering(tmp_path, fake_graph, capsys):
    routes = tmp_path / "routes"
    save_decision(_decision(up_allowed=True), routes)

    assert cli.main(_argv(routes)) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert out.startswith("ALLOWED: NVDA as of 2026-09-30")
    assert "No order will be placed." in out
    assert len(FakeGraph.instances) == 1


@pytest.mark.unit
def test_cli_defaults_to_the_market_analyst_only(tmp_path, fake_graph):
    routes = tmp_path / "routes"
    save_decision(_decision(), routes)

    cli.main(_argv(routes))

    assert FakeGraph.instances[0].analysts == ("market",)


@pytest.mark.unit
def test_cli_passes_chosen_analysts(tmp_path, fake_graph):
    routes = tmp_path / "routes"
    save_decision(_decision(), routes)

    cli.main(_argv(routes, "NVDA", "--analysts", "market", "news"))

    assert FakeGraph.instances[0].analysts == ("market", "news")


@pytest.mark.unit
def test_cli_stops_with_no_run_for_a_ticker_outside_the_route(tmp_path, fake_graph, capsys):
    routes = tmp_path / "routes"
    save_decision(_decision(), routes)

    assert cli.main(_argv(routes, "AAPL")) == cli.EXIT_FAILED

    assert "nothing was run" in capsys.readouterr().err
    assert FakeGraph.instances == []


@pytest.mark.unit
def test_cli_stops_with_no_run_when_the_route_is_missing(tmp_path, fake_graph, capsys):
    assert cli.main(_argv(tmp_path / "routes")) == cli.EXIT_FAILED

    assert "no route decision for 2026-09-30" in capsys.readouterr().err
    assert FakeGraph.instances == []


@pytest.mark.unit
def test_cli_reports_a_failed_run(tmp_path, monkeypatch, capsys):
    routes = tmp_path / "routes"
    save_decision(_decision(), routes)

    class Broken(FakeGraph):
        def propagate(self, ticker, trade_date):
            raise RuntimeError("LLM timed out")

    monkeypatch.setattr(research_runner, "make_graph", lambda analysts: Broken(analysts))

    assert cli.main(_argv(routes)) == cli.EXIT_FAILED
    assert "TradingAgents run failed: LLM timed out" in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.parametrize("argv", [
    ["--as-of", "30/09/2026", "--ticker", "NVDA"],
    ["--as-of", "2026-09-30", "--ticker", "NVDA;rm"],
])
def test_cli_refuses_bad_input(argv, fake_graph):
    assert cli.main(argv) == cli.EXIT_BAD_INPUT
    assert FakeGraph.instances == []


@pytest.mark.unit
def test_cli_refuses_an_unknown_analyst(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(_argv(tmp_path, "NVDA", "--analysts", "astrology"))
