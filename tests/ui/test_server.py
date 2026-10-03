"""The UI server reads the pipeline's files, runs whitelisted scripts, and maps the paper account."""

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from ui.server import broker, jobs, paths  # noqa: E402
from ui.server.app import app  # noqa: E402

AS_OF = "2026-09-30"


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _route(label: str, created_at: str) -> dict:
    return {
        "as_of": AS_OF, "created_at": created_at, "series": "ALL", "regime_symbol": "SPY",
        "regime_label": label, "up_allowed": label == "UP", "down_allowed": label == "DOWN",
        "tradeable": [] if label == "SIDE" else ["NVDA"], "blocked": ["NVDA"] if label == "SIDE" else [],
        "up_passed": ["NVDA"], "down_passed": [], "reason": f"SPY regime is {label}",
        "basket_file": None, "down_basket_file": None, "regime_file": "x.json", "schema_version": 1,
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    monkeypatch.delenv("TRADINGAGENTS_RESULTS_DIR", raising=False)
    _write(tmp_path / "regimes" / f"regime_SPY_{AS_OF}_20261001T000000Z.json",
           {"symbol": "SPY", "as_of": AS_OF, "created_at": "2026-10-01T00:00:00+00:00", "label": "SIDE",
            "reasons": ["ADX 10.7 is under 20"], "metrics": {"adx_14": 10.7}, "schema_version": 1})
    _write(tmp_path / "baskets" / f"scan-upward-trend-momentum_{AS_OF}_20261001T000000Z.json",
           {"scan_name": "scan-upward-trend-momentum", "as_of": AS_OF, "created_at": "2026-10-01T00:00:00+00:00",
            "members": [{"ticker": "NVDA", "score": 1.2, "metrics": {}}], "rejected": [], "schema_version": 1})
    # Two routes for the date: the newer created_at wins, whatever the file names say.
    _write(tmp_path / "routes" / f"route_ALL_{AS_OF}_20261003T000000Z.json", _route("SIDE", "2026-10-03T00:00:00+00:00"))
    _write(tmp_path / "routes" / f"route_UP_{AS_OF}_20261001T000000Z.json", _route("UP", "2026-10-01T00:00:00+00:00"))
    state = tmp_path / "logs" / "NVDA" / "TradingAgentsStrategy_logs" / f"full_states_log_{AS_OF}.json"
    _write(state, {"company_of_interest": "NVDA", "final_rating": "Hold", "market_report": "m"})
    report = tmp_path / "logs" / "reports" / "NVDA_20261001_204137" / "5_portfolio"
    report.mkdir(parents=True)
    (report / "decision.md").write_text("Hold", encoding="utf-8")
    return tmp_path


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.unit
def test_summary_picks_newest_route_by_created_at(home, client):
    body = client.get("/api/summary").json()
    assert body["as_of"] == AS_OF
    assert body["regime"]["data"]["label"] == "SIDE"
    assert body["route"]["file"].startswith("route_ALL_")
    assert [m["ticker"] for m in body["baskets"]["up"]["data"]["members"]] == ["NVDA"]
    assert body["baskets"]["down"] is None


@pytest.mark.unit
def test_research_runs_and_reports_are_listed(home, client):
    assert client.get("/api/research/runs").json()[0]["rating"] == "Hold"
    assert client.get(f"/api/research/state/NVDA/{AS_OF}").json()["market_report"] == "m"
    sections = client.get("/api/research/reports/NVDA_20261001_204137").json()["sections"]
    assert sections == {"5_portfolio/decision.md": "Hold"}


@pytest.mark.unit
@pytest.mark.parametrize("url", [
    "/api/baskets/..%2F..%2F.env",
    "/api/research/state/NVDA/..",
    "/api/research/reports/..",
])
def test_paths_outside_the_data_folders_are_refused(home, client, url):
    assert client.get(url).status_code in (404, 422)


@pytest.mark.unit
def test_bad_input_never_reaches_a_script(home, client):
    assert client.post("/api/bots/run", json={"as_of": AS_OF, "bot": "up", "symbol": "NVDA; rm -rf"}).status_code == 400
    assert client.post("/api/bots/run", json={"as_of": AS_OF, "bot": "trade", "symbol": "NVDA"}).status_code == 400
    assert client.post("/api/scans/run", json={"as_of": "yesterday"}).status_code == 400
    assert client.get("/api/jobs").json() == [] or all(j["kind"] != "bot:trade" for j in client.get("/api/jobs").json())


@pytest.mark.unit
def test_research_needs_confirmation(home, client):
    resp = client.post("/api/research/run", json={"as_of": AS_OF, "ticker": "NVDA", "analysts": ["market"]})
    assert resp.status_code == 400


@pytest.mark.unit
def test_step_refuses_scripts_outside_the_whitelist():
    with pytest.raises(ValueError):
        jobs.Step("place_order", [])


@pytest.mark.unit
def test_bots_run_as_scripts_and_a_regime_mismatch_is_blocked(home, client):
    """The newest route is SIDE: SIDEBot writes a plan, UPBot exits 3 and writes nothing."""
    side = client.post("/api/bots/run", json={"as_of": AS_OF, "bot": "side", "symbol": "NVDA"}).json()
    up = client.post("/api/bots/run", json={"as_of": AS_OF, "bot": "up", "symbol": "NVDA"}).json()
    side_job = jobs.wait_for(jobs.runner.get(side["id"]))
    up_job = jobs.wait_for(jobs.runner.get(up["id"]))
    assert (side_job.status, side_job.exit_code) == ("ok", 0), side_job.lines
    assert (up_job.status, up_job.exit_code) == ("blocked", 3), up_job.lines

    plans = client.get("/api/plans").json()
    assert [(p["bot"], p["symbol"]) for p in plans] == [("sidebot", "NVDA")]
    assert plans[0]["fields"]["regime"] == "SIDE (SPY)"

    stream = client.get(f"/api/jobs/{side['id']}/stream").text
    assert "event: line" in stream and "event: done" in stream


class FakeTradingClient:
    def get_account(self):
        return {"equity": "101000", "last_equity": "100000", "buying_power": "200000", "cash": "50000"}

    def get_all_positions(self):
        return [{"symbol": "UPST", "side": "long", "qty": "55", "avg_entry_price": "52.34",
                 "market_value": "1254.55", "unrealized_intraday_pl": "-10", "unrealized_intraday_plpc": "-0.008",
                 "unrealized_pl": "-1624.15", "unrealized_plpc": "-0.5642"}]

    def get_orders(self, request):
        return [{"symbol": "MSFT", "position_intent": "sell_to_close", "side": "sell", "qty": "5",
                 "filled_qty": "5", "filled_avg_price": "533.4", "status": "filled",
                 "submitted_at": "2025-10-27T14:11:21Z", "filled_at": "2025-10-27T14:11:22Z"},
                {"symbol": "TEM", "position_intent": None, "side": "buy", "qty": "2", "filled_qty": "0",
                 "filled_avg_price": None, "status": "canceled", "submitted_at": "2025-10-28T14:00:00Z",
                 "filled_at": None}]

    def get_portfolio_history(self, request):
        return {"timestamp": [1, 2], "equity": [100000, 101000], "profit_loss": [0, 1000],
                "profit_loss_pct": [0, 0.01], "base_value": 100000, "timeframe": "1D"}


@pytest.mark.unit
def test_broker_maps_the_paper_account(monkeypatch, client):
    monkeypatch.setattr(broker, "_broker", broker.ReadOnlyBroker(client=FakeTradingClient()))
    account = client.get("/api/broker/account").json()
    assert account["daily_change"] == 1000 and account["daily_change_pct"] == pytest.approx(1.0)

    [position] = client.get("/api/broker/positions").json()
    assert position["side"] == "Long" and position["total_pl_pct"] == pytest.approx(-56.42)

    orders = client.get("/api/broker/orders").json()
    assert [(o["position_side"], o["side"], o["filled"]) for o in orders] == [("Long", "Sell", True), (None, "Buy", False)]

    history = client.get("/api/broker/history?range=1M").json()
    assert history["points"][-1] == {"t": 2, "equity": 101000, "pnl": 1000, "pnl_pct": 1.0}
    assert client.get("/api/broker/history?range=5Y").status_code == 400


@pytest.mark.unit
def test_missing_keys_is_a_503_not_a_crash(monkeypatch, client):
    def no_keys():
        raise broker.MissingAlpacaKeys("set ALPACA_API_KEY")

    monkeypatch.setattr(broker, "get_broker", no_keys)
    resp = client.get("/api/broker/account")
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "missing_keys"
