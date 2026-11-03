"""Trade page Tradebot routes: the queue read, and each action starting exactly its command as a job."""

import pytest

pytest.importorskip("fastapi")

from datetime import date  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from extensions.tradebot import store  # noqa: E402
from ui.server import jobs, paths  # noqa: E402
from ui.server.app import app  # noqa: E402
from ui.server.routes import tradebot  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    item = store.new_item(ticker="VST", as_of="2026-10-09", kind="up_cat", scan="s", scan_file="f.json",
                          side="long", needs_regime="UP", metrics={}, source="auto")
    store.enqueue("trade", [item])
    return item["id"]


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
            return {"id": "job"}

    def start(kind, steps, group=None):
        calls.append((kind, [(s.module, s.args, s.always) for s in steps], group))
        return FakeJob(steps)

    monkeypatch.setattr(jobs.runner, "start", start)
    return calls


@pytest.mark.unit
def test_the_queue_shows_status_and_sessions_left(home, client):
    [row] = client.get("/api/tradebot/queue").json()
    assert row["id"] == home and row["status"] == "waiting" and row["bot"] == "UP"
    assert row["expires_after"] == "2026-10-23" and row["last_check"] is None and row["last_guard"] is None
    assert isinstance(row["sessions_left"], int)


@pytest.mark.unit
def test_sessions_left_counts_trading_days_through_the_last_one():
    assert tradebot.sessions_left("2026-10-23", date(2026, 10, 19)) == 5  # Mon..Fri
    assert tradebot.sessions_left("2026-10-23", date(2026, 10, 26)) == 0
    assert tradebot.sessions_left(None, date(2026, 10, 19)) is None


@pytest.mark.unit
@pytest.mark.parametrize("stage", ["eod", "intraday"])
def test_check_starts_run_tradebot_at_the_stage(home, client, started, stage):
    client.post("/api/tradebot/check", json={"stage": stage})
    client.post("/api/tradebot/check", json={"stage": stage, "item": home})
    assert started == [
        (f"tradebot:check-{stage}", [("extensions.scripts.run_tradebot", ["--stage", stage], False)], "pipeline"),
        (f"tradebot:check-{stage}", [("extensions.scripts.run_tradebot", ["--stage", stage, "--item", home], False)],
         "pipeline")]


@pytest.mark.unit
def test_a_bad_stage_or_unknown_item_starts_nothing(home, client, started):
    assert client.post("/api/tradebot/check", json={"stage": "lunch"}).status_code == 400
    assert client.post("/api/tradebot/ticket", json={"item": "nope"}).status_code == 400
    assert started == []


@pytest.mark.unit
def test_ticket_and_preview_start_their_commands(home, client, started):
    client.post("/api/tradebot/ticket", json={})
    client.post("/api/tradebot/preview", json={})
    assert started[0] == ("tradebot:ticket", [("extensions.scripts.run_tradebot", ["--ticket-only"], False)], "pipeline")
    assert started[1] == ("tradebot:preview", [
        ("extensions.scripts.run_tradebot", ["--ticket-only", "--dry-run"], False),
        ("paper_orders.sender", ["--tradebot", "--preview"], True)], "sender")


@pytest.mark.unit
def test_a_send_needs_confirm_and_otherwise_is_the_preview(home, client, started):
    assert client.post("/api/tradebot/send", json={"send": True}).status_code == 400
    client.post("/api/tradebot/send", json={"send": False})
    client.post("/api/tradebot/send", json={"send": True, "confirm": True})
    assert [(k, steps[0][1]) for k, steps, _ in started] == [
        ("tradebot:send-preview", ["--tradebot", "--preview"]), ("tradebot:send", ["--tradebot", "--send"])]
    assert all(g == "sender" for *_, g in started)
