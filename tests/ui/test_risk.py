"""Risk settings, the kill switch and the flatten route. No route talks to the broker; flatten is a job."""

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from paper_orders import guard  # noqa: E402
from ui.server import jobs, paths  # noqa: E402
from ui.server.app import app  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    return tmp_path


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def started(monkeypatch):
    calls = []

    class FakeJob:
        def __init__(self, steps):
            self.steps = steps

        def summary(self):
            return {"id": "job", "steps": [{"script": s.script, "args": s.args} for s in self.steps]}

    def start(kind, steps, group=None):
        calls.append((kind, steps, group))
        return FakeJob(steps)

    monkeypatch.setattr(jobs.runner, "start", start)
    return calls


@pytest.mark.unit
def test_the_risk_defaults_show_with_no_settings_file(home, client):
    body = client.get("/api/risk").json()
    assert body["risk"] == guard.validate_risk({}) and body["kill_switch"] is False and body["error"] is None
    assert client.get("/api/settings").json()["risk"]["max_positions"] == 15


@pytest.mark.unit
def test_limits_save_and_the_guard_reads_them(home, client):
    saved = client.put("/api/settings/risk", json={"daily_loss_pct": 2, "max_positions": 8}).json()
    assert saved["daily_loss_pct"] == 2.0 and saved["max_positions"] == 8 and saved["drawdown_pct"] == 10.0
    assert guard.load_risk()["max_positions"] == 8


@pytest.mark.unit
def test_saving_limits_never_changes_the_kill_switch(home, client):
    client.put("/api/risk/kill-switch", json={"on": True, "reason": "x"})
    assert client.put("/api/settings/risk", json={"kill_switch": False, "drawdown_pct": 5}).status_code == 200
    assert guard.load_risk()["kill_switch"] is True


@pytest.mark.unit
@pytest.mark.parametrize("bad", [{"daily_loss_pct": 0}, {"max_positions": "8"}, {"nope": 1}])
def test_a_bad_limit_is_refused(home, client, bad):
    assert client.put("/api/settings/risk", json=bad).status_code == 400


@pytest.mark.unit
def test_the_kill_switch_turns_on_and_off_with_its_reason(home, client):
    on = client.put("/api/risk/kill-switch", json={"on": True, "reason": "testing"}).json()
    assert on["kill_switch"] is True and on["kill_switch_reason"] == "testing"
    assert guard.load_risk()["kill_switch"] is True
    off = client.put("/api/risk/kill-switch", json={"on": False, "reason": ""}).json()
    assert off["kill_switch"] is False and guard.load_risk()["kill_switch"] is False


@pytest.mark.unit
def test_a_flatten_send_needs_confirm_and_starts_nothing_without_it(home, client, started):
    assert client.post("/api/risk/flatten", json={"send": True}).status_code == 400
    assert started == [] and guard.load_risk()["kill_switch"] is False


@pytest.mark.unit
def test_a_confirmed_flatten_turns_the_kill_switch_on_then_starts_the_job(home, client, started):
    job = client.post("/api/risk/flatten", json={"send": True, "confirm": True, "reason": "test"}).json()
    assert guard.load_risk()["kill_switch"] is True
    [(kind, steps, group)] = started
    assert kind == "risk:flatten" and group == "flatten" and steps[0].script == jobs.FLATTEN
    assert steps[0].args == ["--reason", "test", "--send"] and steps[0].module == "paper_orders.flatten"
    assert job["steps"][0]["script"] == jobs.FLATTEN


@pytest.mark.unit
def test_a_flatten_preview_changes_nothing(home, client, started):
    client.post("/api/risk/flatten", json={"send": False})
    [(kind, steps, _)] = started
    assert kind == "risk:flatten-preview" and "--send" not in steps[0].args
    assert guard.load_risk()["kill_switch"] is False


@pytest.mark.unit
def test_a_broken_risk_section_is_reported(home, client):
    (home / "settings.json").write_text(json.dumps({"risk": {"drawdown_pct": "ten"}}), encoding="utf-8")
    body = client.get("/api/risk").json()
    assert body["risk"] is None and "drawdown_pct" in body["error"]
