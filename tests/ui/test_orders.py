"""The ledger and reconcile routes read files; the run route only starts the read-only reconcile job."""

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from paper_orders import ledger  # noqa: E402
from ui.server import jobs, paths  # noqa: E402
from ui.server.app import app  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.unit
def test_the_ledger_route_returns_the_month_newest_first(home, client):
    ledger.append([ledger.line("daily", "entry", "A"), ledger.line("daily", "exit", "A")])
    lines = client.get("/api/orders/ledger").json()["lines"]
    assert [r["kind"] for r in lines] == ["exit", "entry"]
    assert client.get("/api/orders/ledger?month=2026-13").status_code == 400


@pytest.mark.unit
def test_the_reconcile_route_returns_the_newest_report(home, client):
    assert client.get("/api/orders/reconcile").json() == {"file": None, "report": None}
    folder = home / "status"
    folder.mkdir()
    for stamp, clean in (("20261102T200000Z", False), ("20261103T200000Z", True)):
        (folder / f"reconcile_{stamp}.json").write_text(json.dumps({"clean": clean}), encoding="utf-8")
    body = client.get("/api/orders/reconcile").json()
    assert body["file"] == "reconcile_20261103T200000Z.json" and body["report"]["clean"] is True


@pytest.mark.unit
def test_the_run_route_starts_only_the_reconcile_job(home, client, monkeypatch):
    calls = []

    class FakeJob:
        def summary(self):
            return {"id": "job"}

    monkeypatch.setattr(jobs.runner, "start", lambda kind, steps, group=None: calls.append((kind, steps, group))
                        or FakeJob())
    client.post("/api/orders/reconcile/run")
    [(kind, steps, group)] = calls
    assert kind == "orders:reconcile" and group == "reconcile"
    assert [s.module for s in steps] == ["paper_orders.reconcile"] and steps[0].args == []
