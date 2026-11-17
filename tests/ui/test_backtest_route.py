"""GET /api/backtest: a read-only replay over made-up files in a temp data home."""

import json
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from ui.server import paths  # noqa: E402
from ui.server.app import app  # noqa: E402

pytestmark = pytest.mark.unit
AS_OF, STAMP = "2026-03-02", "20260302T220000Z"


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    monkeypatch.setenv("BATS_SCHEDULER", "0")
    _write(tmp_path / "routes" / f"route_ALL_{AS_OF}_{STAMP}.json",
           {"as_of": AS_OF, "regime_label": "SIDE", "up_passed": ["ZZZ"], "down_passed": [], "basket_file": None,
            "schema_version": 1})
    channel = f"long_channel_{AS_OF}_{STAMP}.json"
    _write(tmp_path / "scanbot" / channel, {
        "as_of": AS_OF, "members": ["AAA"], "stage_counts": {"pivot_confirmed": 1},
        "calls": {"AAA": {"metrics": {"support": 100.0, "resistance": 110.0, "atr_14": 2.0, "stop_swing_low": {
            "price": 98.0, "date": "2026-02-10", "rule": "clustered_within_3atr_beyond_line"}}}}})
    _write(tmp_path / "handoffs" / f"ranked_side_handoff_{AS_OF}_{STAMP}.json", {
        "as_of": AS_OF, "long_channel_report": channel, "names": {"AAA": {
            "side": "long", "status": "PLAN", "levels": {"support": 100.0, "resistance": 110.0, "midline": 105.0,
                                                         "atr_14": 2.0, "invalidation": 99.5}}}})
    days = pd.bdate_range(start="2026-02-02", end="2026-03-10").date
    rows = [("AAA", str(d), 100.5, 106 if str(d) == "2026-03-04" else 102, 99.8, 101) for d in days]
    bars = tmp_path / "cache" / "bars" / "AAA_2026-01-01_2026-12-31_sip_split.csv"
    bars.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=["symbol", "date", "open", "high", "low", "close"]).to_csv(bars, index=False)
    return TestClient(app)


def test_backtest_returns_trades_summary_and_curve(client):
    resp = client.get("/api/backtest", params={"start": AS_OF, "end": "2026-03-06"})
    assert resp.status_code == 200
    body = resp.json()
    [trade] = body["trades"]
    assert trade["origin"] == "replay"
    assert (trade["symbol"], trade["entry_date"], trade["exit_reason"], trade["exit"]) == \
           ("AAA", "2026-03-03", "target", 105.0)
    assert {s["symbol"]: s["reason"] for s in body["skipped"]} == {"ZZZ": "regime SIDE does not match UP"}
    side = {(row["origin"], row["strategy"]): row for row in body["summary"]}[("replay", "side")]
    assert side["trades"] == 1 and side["win_rate"] == 100.0
    assert body["curve"][0]["replay"] == 100.0 and body["curve"][-1]["replay"] > 100.0
    assert body["params"]["capital"] == 100_000.0
    # live sizing: one name gets min(23% basket, 15% name cap) = $15,000 -> 150 shares at the 100 limit
    assert (trade["sizing"], trade["qty"], trade["entry"]) == ("live", 150, 100.0)
    assert trade["pnl_usd"] == round(150 * (trade["exit"] - trade["entry"]), 2)
    assert side["total_return_usd"] == trade["pnl_usd"]


def test_capital_sets_the_dollar_figures(client):
    body = client.get("/api/backtest", params={"start": AS_OF, "end": "2026-03-06", "capital": 20000,
                                               "risk_pct": 2}).json()
    [trade] = body["trades"]
    assert body["params"]["capital"] == 20000 and trade["qty"] == 30  # 15% of $20,000 at 100
    fixed = client.get("/api/backtest", params={"start": AS_OF, "end": "2026-03-06", "capital": 20000,
                                                "risk_pct": 2, "sizing": "fixed"}).json()["trades"][0]
    assert fixed["qty"] == int(400 // fixed["risk_per_share"])  # 2% of $20,000 at risk a share
    assert fixed["pnl_usd"] == round(fixed["qty"] * (fixed["exit"] - fixed["entry"]), 2)


def test_single_date_defaults_end_to_start(client):
    body = client.get("/api/backtest", params={"start": AS_OF, "strategy": "side"}).json()
    assert body["params"]["end"] == AS_OF and len(body["trades"]) == 1


@pytest.mark.parametrize("params", [
    {"start": "nope"},
    {"start": AS_OF, "end": "2026-03-01"},
    {"start": AS_OF, "strategy": "moon"},
    {"start": AS_OF, "timeframe": "1m"},
    {"start": AS_OF, "max_sessions": 0},
    {"start": AS_OF, "risk_pct": 50},
    {"start": AS_OF, "capital": 10},
    {"start": AS_OF, "sizing": "huge"},
    {"start": AS_OF, "origins": "mars"},
    {"start": AS_OF, "max_positions": 500},
    {"start": AS_OF, "slippage_bps": 900},
    {"start": AS_OF, "buying_power": 50_000},
    {"start": AS_OF, "buying_power": 500_000},
])
def test_bad_requests_are_400(client, params):
    assert client.get("/api/backtest", params=params).status_code == 400


def test_results_have_no_write_method(client):
    assert client.post("/api/backtest", json={"start": AS_OF}).status_code == 405


def test_coverage_lists_each_weekday(client):
    days = client.get("/api/backtest/coverage", params={"start": "2026-03-02", "end": "2026-03-06"}).json()
    assert [(d["date"], d["status"]) for d in days] == [
        ("2026-03-02", "saved"), ("2026-03-03", "missing"), ("2026-03-04", "missing"), ("2026-03-05", "missing"),
        ("2026-03-06", "missing")]


def test_reconstruct_starts_one_job_with_validated_args(client, monkeypatch, tmp_path):
    from ui.server import jobs

    started = []

    class Job:
        def summary(self):
            return {"id": "j1", "kind": "backtest_reconstruct", "status": "running"}

    def start(kind, steps, group=None):
        started.append((kind, [(s.script, s.args) for s in steps], group))
        return Job()

    monkeypatch.setattr(jobs.runner, "start", start)
    resp = client.post("/api/backtest/reconstruct", json={"start": "2026-03-02", "end": "2026-03-06", "rebuild": True})
    assert resp.status_code == 200 and resp.json()["id"] == "j1"
    [(kind, [(script, args)], group)] = started
    assert (kind, script, group) == ("backtest_reconstruct", "run_backtest_reconstruct", "backtest")
    assert args == ["--start", "2026-03-02", "--end", "2026-03-06", "--home", str(tmp_path), "--rebuild"]


def test_reconstruct_refuses_today_and_a_running_rebuild(client, monkeypatch):
    from datetime import date

    from ui.server import jobs

    today = date.today().isoformat()
    assert client.post("/api/backtest/reconstruct", json={"start": today}).status_code == 400

    def busy(*_a, **_k):
        raise jobs.JobConflict("a backtest job is already running")

    monkeypatch.setattr(jobs.runner, "start", busy)
    assert client.post("/api/backtest/reconstruct", json={"start": "2026-03-02"}).status_code == 409


def test_reconstruct_passes_the_sec_download_flag(client, monkeypatch):
    from ui.server import jobs

    started = []

    class Job:
        def summary(self):
            return {"id": "j2"}

    monkeypatch.setattr(jobs.runner, "start", lambda kind, steps, group=None: started.append(steps[0].args) or Job())
    client.post("/api/backtest/reconstruct", json={"start": "2026-03-02", "fetch_fundamentals": True})
    client.post("/api/backtest/reconstruct", json={"start": "2026-03-02"})
    assert "--fetch-fundamentals" in started[0] and "--fetch-fundamentals" not in started[1]


def test_hold_to_exit_is_passed_through(client):
    body = client.get("/api/backtest", params={"start": AS_OF, "hold_to_exit": "true", "max_sessions": 1}).json()
    assert body["params"]["hold_to_exit"] is True
    [trade] = body["trades"]
    assert trade["exit_reason"] == "target" and trade["sessions_held"] == 2  # past the 1-session limit


def test_save_list_open_export_and_delete_a_run(client, tmp_path):
    saved = client.post("/api/backtest/runs", json={"start": AS_OF, "end": "2026-03-06", "name": "Side test",
                                                    "capital": 50000}).json()
    assert saved["name"] == "Side test" and saved["trades"] == 1 and saved["params"]["capital"] == 50000
    assert list((tmp_path / "backtest" / "runs").glob("*.json")) == [tmp_path / "backtest" / "runs" /
                                                                     f"{saved['id']}.json"]
    assert [r["id"] for r in client.get("/api/backtest/runs").json()] == [saved["id"]]

    run = client.get(f"/api/backtest/runs/{saved['id']}").json()
    [trade] = run["result"]["trades"]
    assert trade["symbol"] == "AAA" and trade["qty"] == 75  # 15% of $50,000 at 100

    js = client.get(f"/api/backtest/runs/{saved['id']}/export", params={"format": "json"})
    assert js.headers["content-type"].startswith("application/json")
    assert 'filename="backtest_Side-test_' in js.headers["content-disposition"]
    assert js.json()["result"]["trades"] == run["result"]["trades"]

    trades = client.get(f"/api/backtest/runs/{saved['id']}/export", params={"format": "trades_csv"})
    assert trades.headers["content-type"].startswith("text/csv")
    header, row = trades.text.strip().splitlines()
    assert header.startswith("origin,run_date,bot,symbol") and ",AAA," in row
    summary = client.get(f"/api/backtest/runs/{saved['id']}/export", params={"format": "summary_csv"}).text
    assert "total_return_usd,total_return_usd_ex_best,max_drawdown_pct,peak_positions" in summary.splitlines()[0]
    assert client.get(f"/api/backtest/runs/{saved['id']}/export", params={"format": "xml"}).status_code == 400

    renamed = client.put(f"/api/backtest/runs/{saved['id']}", json={"name": "Renamed"}).json()
    assert renamed["name"] == "Renamed" and client.get("/api/backtest/runs").json()[0]["name"] == "Renamed"
    assert client.put(f"/api/backtest/runs/{saved['id']}", json={"name": ""}).status_code == 422

    assert client.delete(f"/api/backtest/runs/{saved['id']}").json() == {"deleted": saved["id"]}
    assert client.get("/api/backtest/runs").json() == []
    assert client.get(f"/api/backtest/runs/{saved['id']}").status_code == 404


def test_saving_a_bad_run_is_refused_and_ids_cannot_leave_the_runs_folder(client):
    assert client.post("/api/backtest/runs", json={"start": AS_OF, "risk_pct": 50}).status_code == 400
    assert client.get("/api/backtest/runs").json() == []
    for bad in ("..%2Fsettings", "20260302T220000Z-zzzzzz", "nope"):
        assert client.get(f"/api/backtest/runs/{bad}").status_code == 404
        assert client.delete(f"/api/backtest/runs/{bad}").status_code in (404, 405)


def test_default_name_describes_the_run(client):
    saved = client.post("/api/backtest/runs", json={"start": AS_OF, "strategy": "side"}).json()
    assert saved["name"] == f"{AS_OF} to {AS_OF} · side"


def test_origins_filter_and_new_settings_are_passed_through(client):
    body = client.get("/api/backtest", params={"start": AS_OF, "end": "2026-03-06", "origins": "reconstructed",
                                               "slippage_bps": 5, "commission_per_share": 0.01,
                                               "max_positions": 3}).json()
    p = body["params"]
    assert (p["origins"], p["include_reconstructed"], p["slippage_bps"], p["commission_per_share"],
            p["max_positions"], p["max_gross_pct"], p["max_open_risk_pct"]) == ("reconstructed", True, 5, 0.01, 3,
                                                                                60.0, 8.0)
    assert body["trades"] == [] and {r["origin"] for r in body["summary"]} == {"reconstructed"}
    assert any("Starting capital" in a for a in body["assumptions"])


def test_reconstruct_passes_the_trade_bars_flag(client, monkeypatch):
    from ui.server import jobs

    started = []

    class Job:
        def summary(self):
            return {"id": "j3"}

    monkeypatch.setattr(jobs.runner, "start", lambda kind, steps, group=None: started.append(steps[0].args) or Job())
    client.post("/api/backtest/reconstruct", json={"start": "2026-03-02", "trade_bars": True})
    assert "--trade-bars" in started[0]


def test_buying_power_sets_the_basket_and_the_equity_caps_stay(client, tmp_path):
    assert client.get("/api/backtest/defaults").json()["series_original_buying_power"] is None
    _write(tmp_path / "tickets" / "series.json", {"series_version": 1, "original_buying_power": 393393.64,
                                                  "recorded_at": "2026-10-15T21:48:24+00:00"})
    assert client.get("/api/backtest/defaults").json()["series_original_buying_power"] == 393393.64
    base = {"start": AS_OF, "end": "2026-03-06", "capital": 100_000}
    plain = client.get("/api/backtest", params=base).json()
    assert plain["params"]["buying_power"] == 100_000 and plain["trades"][0]["qty"] == 150  # 15% cap of 100k
    # 2 x margin: the 23% basket is $46,000, but one name is still held to 15% of the equity
    margin = client.get("/api/backtest", params={**base, "buying_power": 200_000}).json()
    assert margin["params"]["buying_power"] == 200_000 and margin["trades"][0]["qty"] == 150


def test_tradebot_entry_and_its_switches_are_passed_through(client):
    defaults = client.get("/api/backtest/defaults").json()
    assert set(defaults["tradebot_filters"]) == {"squeeze_release", "rsi_extreme", "light_volume", "squeeze_4h1h"}
    assert isinstance(defaults["tradebot_expire_sessions"], int)
    body = client.get("/api/backtest", params={"start": AS_OF, "end": "2026-03-06", "strategy": "side",
                                               "entry_mode": "tradebot", "squeeze_4h1h": False,
                                               "expire_sessions": 4}).json()
    p = body["params"]
    assert (p["entry_mode"], p["filters"]["squeeze_4h1h"], p["filters"]["rsi_extreme"], p["expire_sessions"]) == \
           ("tradebot", False, True, 4)
    assert body["funnel"]["replay"]["queued"] == 1 and "next_day" in body["comparison"]
    assert {r["strategy"] for r in body["breakdown"]} <= {"long", "short", "stop_swing", "stop_invalidation"}
    assert client.get("/api/backtest", params={"start": AS_OF, "entry_mode": "later"}).status_code == 400
