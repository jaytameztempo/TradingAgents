"""Trade page order entry: manual tickets are written from the queued row, and only a confirmed send starts
the paper sender, on that one ticket. Nothing here reaches Alpaca; the job runner is stubbed."""

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from paper_orders import sender  # noqa: E402
from ui.server import jobs, manual_tickets, paths  # noqa: E402
from ui.server.app import app  # noqa: E402

AS_OF = "2026-10-09"
LONG_FILE = f"long_channel_{AS_OF}_20261011T004405Z.json"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    calls = {"LOGI": {"passed": True, "reasons": [], "detail": [], "quality": {}, "quality_passed": 1,
                      "metrics": {"close": 98.27, "support": 98.4, "resistance": 108.09}}}
    path = tmp_path / "scanbot" / LONG_FILE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"scan_name": "SCAN-LongSidewaysChannel", "as_of": AS_OF,
                                "created_at": "2026-10-11T00:44:05+00:00", "members": ["LOGI"], "calls": calls}),
                    encoding="utf-8")
    return tmp_path


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def started(monkeypatch):
    """Record jobs instead of running the sender."""
    calls = []

    class FakeJob:
        def __init__(self, steps):
            self.steps = steps

        def summary(self):
            return {"id": f"job{len(calls)}", "steps": [{"script": s.script, "args": s.args} for s in self.steps]}

    def start(kind, steps, group=None):
        calls.append((kind, steps, group))
        return FakeJob(steps)

    monkeypatch.setattr(jobs.runner, "start", start)
    return calls


def _queued(client) -> dict:
    client.post("/api/queues/trade", json={"kind": "long", "file": LONG_FILE, "tickers": ["LOGI"]})
    return client.get("/api/queues").json()["trade"][0]


def _ticket(client, item, **over) -> dict:
    body = {"item_id": item["id"], "entry": 98.4, "stop": 96.99, "target": 103.25, "quantity": 10, **over}
    return client.post("/api/trade/manual", json=body)


@pytest.mark.unit
def test_a_ticket_takes_symbol_side_and_date_from_the_queue_and_the_sender_accepts_it(home, client):
    item = _queued(client)
    resp = _ticket(client, item)
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert (row["symbol"], row["side"], row["order_side"], row["as_of"]) == ("LOGI", "long", "buy", AS_OF)
    assert row["stop_offset"] == 1.41 and row["risk_dollars"] == 14.1
    path, data = sender.load_manual(row["name"], paths.tickets_dir())
    assert path.parent == paths.tickets_dir() / "manual" and data["tickets"][0]["entry"] == 98.4
    assert [r["name"] for r in client.get("/api/trade/manual").json()] == [row["name"]]


@pytest.mark.unit
def test_the_ui_and_sender_agree_on_the_file_name():
    assert manual_tickets.NAME.pattern == sender.MANUAL_FILE.pattern
    assert manual_tickets.MANUAL_VERSION == sender.MANUAL_VERSION


@pytest.mark.unit
@pytest.mark.parametrize("over, match", [
    ({"stop": 99.0}, "stop must be below"),
    ({"target": 97.0}, "target must be above"),
    ({"quantity": 0}, "quantity"),
    ({"entry": -1}, "entry must be a price"),
    ({"item_id": "nope"}, "no longer in the trade queue"),
])
def test_bad_tickets_are_refused(home, client, over, match):
    resp = _ticket(client, _queued(client), **over)
    assert resp.status_code == 400 and match in resp.json()["detail"]
    assert client.get("/api/trade/manual").json() == []


@pytest.mark.unit
def test_a_preview_is_a_dry_run_and_a_send_needs_confirm(home, client, started):
    name = _ticket(client, _queued(client)).json()["name"]
    tickets = str(paths.tickets_dir())

    assert client.post(f"/api/trade/manual/{name}/send", json={}).status_code == 200
    assert started[-1][1][0].args == ["--manual", name, "--ticket-dir", tickets] and started[-1][2] == "sender"

    refused = client.post(f"/api/trade/manual/{name}/send", json={"send": True})
    assert refused.status_code == 400 and "confirm" in refused.json()["detail"] and len(started) == 1

    assert client.post(f"/api/trade/manual/{name}/send", json={"send": True, "confirm": True}).status_code == 200
    step = started[-1][1][0]
    assert step.script == jobs.SENDER and step.args == ["--manual", name, "--ticket-dir", tickets, "--send"]
    assert [s["job_id"] for s in client.get("/api/trade/manual").json()[0]["sends"]] == ["job2"]


@pytest.mark.unit
@pytest.mark.parametrize("name", ["..%2Fsettings.json", "paper_tickets_2026-10-09_20261009T003216Z.json",
                                  "manual_ticket_2026-10-09_LOGI_0123456789ab.json"])
def test_send_refuses_anything_but_an_existing_manual_ticket(home, client, started, name):
    resp = client.post(f"/api/trade/manual/{name}/send", json={"send": True, "confirm": True})
    assert resp.status_code in (400, 404, 405) and started == []


@pytest.mark.unit
def test_an_unsent_ticket_can_be_deleted_but_a_sent_one_stays(home, client, started):
    item = _queued(client)
    first, second = _ticket(client, item).json()["name"], _ticket(client, item).json()["name"]
    assert client.delete(f"/api/trade/manual/{first}").status_code == 200
    client.post(f"/api/trade/manual/{second}/send", json={"send": True, "confirm": True})
    resp = client.delete(f"/api/trade/manual/{second}")
    assert resp.status_code == 400 and "stays" in resp.json()["detail"]
    assert [r["name"] for r in client.get("/api/trade/manual").json()] == [second]


@pytest.mark.unit
def test_a_second_send_while_one_runs_is_a_conflict(home, client, monkeypatch):
    name = _ticket(client, _queued(client)).json()["name"]

    def busy(kind, steps, group=None):
        raise jobs.JobConflict("a sender job is already running")

    monkeypatch.setattr(jobs.runner, "start", busy)
    assert client.post(f"/api/trade/manual/{name}/send", json={}).status_code == 409
    assert "sender" in jobs.EXCLUSIVE_GROUPS  # so a manual send never overlaps the scheduled one
