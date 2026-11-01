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
    # AS_OF already has its scan inputs, so a scan for it starts the scan steps only (see input_steps).
    _write(tmp_path / "scanbot" / f"financials_{AS_OF}_20261001T000000Z.json", {"as_of": AS_OF})
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
    assert "event: line" in stream and "event: done" in stream and "event: steps" in stream


@pytest.mark.unit
def test_a_job_records_when_each_step_started_and_finished(home, client):
    """The job log's step counter and timers read these; the step that failed stops the rest, which never start."""
    job = jobs.runner.start("test", [jobs.Step("run_router", ["--as-of", "not-a-date"]),
                                     jobs.Step("run_router", ["--as-of", AS_OF])])
    jobs.wait_for(job)
    first, second = job.summary()["steps"]
    assert first["started_at"] and first["finished_at"] and first["started_at"] <= first["finished_at"]
    assert first["exit_code"] == 2 and second["started_at"] is None and second["finished_at"] is None
    assert job.summary()["current_step"] is None

    running = jobs.Job(id="x", kind="k", group=None, steps=[jobs.Step("run_router", []), jobs.Step("run_router", [])])
    running.steps[0].started_at = running.steps[0].finished_at = "2026-11-01T00:00:00+00:00"
    running.steps[1].started_at = "2026-11-01T00:00:05+00:00"
    assert running.summary()["current_step"] == 1


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


@pytest.mark.unit
@pytest.mark.parametrize(("kind", "scripts"), [
    ("long_side", ["run_scanbot_long_channel"]),
    ("short_side", ["run_scanbot_short_channel"]),
    ("sideways", ["run_scanbot_long_channel", "run_scanbot_short_channel"]),
])
def test_scan_kinds_start_the_matching_scripts(home, client, monkeypatch, kind, scripts):
    started = []

    def fake_start(kind_name, steps, group=None):
        started.extend(steps)
        return jobs.Job(id="x", kind=kind_name, group=group, steps=steps)

    monkeypatch.setattr(jobs.runner, "start", fake_start)
    body = {"as_of": AS_OF, "kind": kind, "tickers": ["NVDA"]}
    assert client.post("/api/scans/run", json=body).status_code == 200
    assert [s.script for s in started] == scripts
    scanbot = str(paths.scanbot_dir())
    for step in started:
        # Channel scans scan the financials survivors: no ticker list, report dir and out dir = scanbot.
        assert step.args == ["--as-of", AS_OF, "--report-dir", scanbot, "--out-dir", scanbot]


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["up", "down", "both", "all"])
def test_the_retired_five_name_trend_kinds_are_refused(home, client, monkeypatch, kind):
    started = _capture_steps(monkeypatch)
    assert client.post("/api/scans/run", json={"as_of": AS_OF, "kind": kind}).status_code == 400
    assert started == []
    assert not {"run_upward_trend_scan", "run_breakdown_scan"} & jobs.SCRIPTS


@pytest.mark.unit
def test_the_pipeline_runs_the_category_scans_then_regime_then_the_router(home, client, monkeypatch):
    started = _capture_steps(monkeypatch)
    assert client.post("/api/pipeline/run", json={"as_of": AS_OF, "category": 2}).status_code == 200
    assert [s.script for s in started] == ["run_category_scan", "run_category_scan", "run_regime_bot", "run_router"]
    assert [s.args[1] for s in started[:2]] == ["up", "down"]
    assert all(s.args[s.args.index("--category") + 1] == "2" for s in (started[0], started[1], started[3]))
    assert client.post("/api/pipeline/run", json={"as_of": AS_OF, "category": 5}).status_code == 400


@pytest.mark.unit
def test_the_router_runs_on_the_chosen_category(home, client, monkeypatch):
    started = _capture_steps(monkeypatch)
    assert client.post("/api/router/run", json={"as_of": AS_OF}).status_code == 200
    assert client.post("/api/router/run", json={"as_of": AS_OF, "category": 3}).status_code == 200
    assert [s.args[:4] for s in started] == [["--as-of", AS_OF, "--category", "1"], ["--as-of", AS_OF, "--category", "3"]]
    assert client.post("/api/router/run", json={"as_of": AS_OF, "category": 0}).status_code == 400


def _capture_steps(monkeypatch) -> list:
    started = []

    def fake_start(kind_name, steps, group=None):
        started.extend(steps)
        return jobs.Job(id="x", kind=kind_name, group=group, steps=steps)

    monkeypatch.setattr(jobs.runner, "start", fake_start)
    return started


@pytest.mark.unit
@pytest.mark.parametrize(("kind", "scans"), [
    ("cat_up", ["up"]), ("cat_down", ["down"]), ("cat_side", ["side"]), ("cat_all", ["up", "down", "side"]),
])
@pytest.mark.parametrize("category", [1, 2, 3])
def test_category_kinds_start_run_category_scan_with_the_category(home, client, monkeypatch, kind, scans, category):
    started = _capture_steps(monkeypatch)
    body = {"as_of": AS_OF, "kind": kind, "tickers": ["NVDA"], "category": category}
    assert client.post("/api/scans/run", json=body).status_code == 200
    assert {s.script for s in started} == {"run_category_scan"} and "run_category_scan" in jobs.SCRIPTS
    scanbot, baskets = str(paths.scanbot_dir()), str(paths.baskets_dir())
    assert [s.args for s in started] == [
        ["--scan", scan, "--as-of", AS_OF, "--category", str(category), "--report-dir", scanbot, "--out-dir", baskets]
        for scan in scans
    ]  # no ticker list: category scans read the financials report


NEW_DATE = "2026-10-30"


@pytest.mark.unit
@pytest.mark.parametrize("kind, scans", [
    ("cat_all", ["run_category_scan"] * 3), ("sideways", ["run_scanbot_long_channel", "run_scanbot_short_channel"]),
])
def test_a_date_without_inputs_builds_the_universe_and_financials_first(home, client, monkeypatch, kind, scans):
    started = _capture_steps(monkeypatch)
    assert client.post("/api/scans/run", json={"as_of": NEW_DATE, "kind": kind}).status_code == 200
    assert [s.script for s in started] == ["run_scanbot_universe", "run_scanbot_financials", *scans]
    scanbot = str(paths.scanbot_dir())
    assert started[0].args == ["--as-of", NEW_DATE, "--assets-dir", str(paths.data_home() / "assets"),
                               "--out-dir", scanbot]
    assert started[1].args == ["--as-of", NEW_DATE, "--report-dir", scanbot, "--out-dir", scanbot]
    assert {"run_scanbot_universe", "run_scanbot_financials"} <= jobs.SCRIPTS


@pytest.mark.unit
def test_a_date_with_a_universe_report_only_needs_the_financials_step(home, client, monkeypatch):
    _write(paths.scanbot_dir() / f"universe_{NEW_DATE}_20261031T000000Z.json", {"as_of": NEW_DATE})
    started = _capture_steps(monkeypatch)
    assert client.post("/api/pipeline/run", json={"as_of": NEW_DATE}).status_code == 200
    assert [s.script for s in started] == ["run_scanbot_financials", "run_category_scan", "run_category_scan",
                                           "run_regime_bot", "run_router"]


@pytest.mark.unit
def test_category_defaults_to_1(home, client, monkeypatch):
    started = _capture_steps(monkeypatch)
    assert client.post("/api/scans/run", json={"as_of": AS_OF, "kind": "cat_up"}).status_code == 200
    assert started[0].args[started[0].args.index("--category") + 1] == "1"


@pytest.mark.unit
@pytest.mark.parametrize("category", [0, 4, -1, "x"])
def test_bad_categories_are_refused(home, client, monkeypatch, category):
    started = _capture_steps(monkeypatch)
    resp = client.post("/api/scans/run", json={"as_of": AS_OF, "kind": "cat_side", "category": category})
    assert resp.status_code in (400, 422) and started == []


@pytest.mark.unit
def test_sideways_kinds_ignore_the_category(home, client, monkeypatch):
    started = _capture_steps(monkeypatch)
    body = {"as_of": AS_OF, "kind": "sideways", "category": 3}
    assert client.post("/api/scans/run", json=body).status_code == 200
    assert [s.script for s in started] == ["run_scanbot_long_channel", "run_scanbot_short_channel"]
    assert all("--category" not in s.args for s in started)


@pytest.mark.unit
def test_category_baskets_are_listed_by_kind_and_category(home, client):
    def basket(name, scan_name):
        _write(paths.baskets_dir() / name, {"scan_name": scan_name, "as_of": AS_OF,
                                            "created_at": "2026-10-01T00:00:00+00:00", "members": [], "rejected": []})
    basket(f"scan-up-category-1_{AS_OF}_20261001T000000Z.json", "SCAN-UP Category 1")
    basket(f"scan-up-category-2_{AS_OF}_20261001T000000Z.json", "SCAN-UP Category 2")
    basket(f"scan-side-long-category-2_{AS_OF}_20261001T000000Z.json", "SCAN-SIDE Long Category 2")
    basket(f"scan-upward-trend-momentum_{AS_OF}_20261001T000000Z.json", "SCAN-Upward Trend Momentum")

    def files(query):
        return [e["file"].split("_")[0] for e in client.get(f"/api/baskets?{query}").json()]
    assert sorted(files("kind=up_cat")) == ["scan-up-category-1", "scan-up-category-2"]
    assert files("kind=up_cat&category=2") == ["scan-up-category-2"]
    assert files("kind=side_long_cat&category=2") == ["scan-side-long-category-2"]
    assert files("kind=up") == ["scan-upward-trend-momentum"]  # the old tab never shows category baskets
    assert client.get("/api/baskets?kind=up_cat&category=4").status_code == 400
    assert client.get("/api/baskets?kind=up&category=1").status_code == 400
    summary = client.get(f"/api/summary?as_of={AS_OF}").json()
    assert set(summary["baskets"]) == {"up", "down"}  # the dashboard summary is unchanged


@pytest.mark.unit
def test_unknown_scan_kind_is_refused(home, client):
    assert client.post("/api/scans/run", json={"as_of": AS_OF, "kind": "orders"}).status_code == 400


@pytest.mark.unit
def test_channel_reports_list_members_calls_only(home, client):
    calls = {t: {"passed": t == "AKAM", "reasons": [], "detail": [], "quality": {}, "quality_passed": 0,
                 "metrics": {"close": 1.0}} for t in ("AKAM", "AAP", "ABC")}
    _write(paths.scanbot_dir() / f"long_channel_{AS_OF}_20261001T000000Z.json",
           {"scan_name": "SCAN-LongSidewaysChannel", "as_of": AS_OF, "created_at": "2026-10-01T00:00:00+00:00",
            "stage_counts": {"bars": 3}, "reason_counts": {}, "quality_counts": {}, "members": ["AKAM"],
            "calls": calls, "report_version": 1})
    body = client.get("/api/channel-scans?side=long").json()
    assert len(body) == 1
    data = body[0]["data"]
    assert data["members"] == ["AKAM"] and list(data["member_calls"]) == ["AKAM"]
    assert data["scanned"] == 3 and "calls" not in data
    assert client.get("/api/channel-scans?side=short").json() == []
    assert client.get("/api/channel-scans?side=orders").status_code == 400


@pytest.mark.unit
def test_no_whitelisted_script_sends_orders():
    """The fork's order sender lives in paper_orders/; nothing the UI may start is an order or ticket step."""
    assert not {s for s in jobs.SCRIPTS if any(w in s for w in ("order", "ticket", "send"))}


@pytest.mark.unit
@pytest.mark.parametrize(("name", "bot", "symbol"), [
    ("sidebot_NVDA_2026-09-30_20261003T003558Z.md", "sidebot", "NVDA"),
    ("side_research_AFRM_2026-09-30_20261008T104134Z.md", "side_research", "AFRM"),
    ("ranked_side_research_BRK.B_2026-10-09_20261011T010016Z.md", "ranked_side_research", "BRK.B"),
])
def test_plan_kind_and_symbol_come_from_the_file_name(tmp_path, name, bot, symbol):
    from ui.server import files

    path = tmp_path / name
    path.write_text("# Research plan\n- Market regime: SIDE (SPY+QQQ)\n", encoding="utf-8")
    plan = files.parse_plan(path)
    assert (plan["bot"], plan["symbol"]) == (bot, symbol)
