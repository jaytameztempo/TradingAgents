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
