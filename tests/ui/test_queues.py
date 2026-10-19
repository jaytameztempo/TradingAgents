"""Scans page "Send to": research / strategy / trade queues and permanent Trash."""

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from ui.server import paths  # noqa: E402
from ui.server.app import app  # noqa: E402

AS_OF = "2026-10-09"
UP_FILE = f"scan-upward-trend-momentum_{AS_OF}_20261010T000000Z.json"
LONG_FILE = f"long_channel_{AS_OF}_20261011T004405Z.json"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    _write(tmp_path / "baskets" / UP_FILE, {
        "scan_name": "scan-upward-trend-momentum", "as_of": AS_OF, "created_at": "2026-10-10T00:00:00+00:00",
        "members": [{"ticker": "NVDA", "score": 0.07, "metrics": {"close": 234.0, "sma_50": 218.3, "sma_200": 200.7}},
                    {"ticker": "MSFT", "score": 0.06, "metrics": {"close": 517.3, "sma_50": 487.7, "sma_200": 432.8}}],
        "rejected": [{"ticker": "SPY", "reason": "below the 50-day"}], "schema_version": 1})
    calls = {t: {"passed": True, "reasons": [], "detail": [], "quality": {}, "quality_passed": 1,
                 "metrics": {"close": 98.27, "support": 98.4, "resistance": 108.09}} for t in ("LOGI", "IDXX")}
    _write(tmp_path / "scanbot" / LONG_FILE, {
        "scan_name": "SCAN-LongSidewaysChannel", "as_of": AS_OF, "created_at": "2026-10-11T00:44:05+00:00",
        "members": ["LOGI", "IDXX"], "calls": calls, "stage_counts": {}, "reason_counts": {}, "quality_counts": {}})
    _write(tmp_path / "routes" / f"route_ALL_{AS_OF}_20261010T221750Z.json", {
        "as_of": AS_OF, "created_at": "2026-10-10T22:17:50+00:00", "regime_symbol": "SPY+QQQ", "regime_label": "SIDE",
        "tradeable": [], "blocked": ["NVDA"], "schema_version": 1})
    return tmp_path


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.unit
def test_a_whole_basket_goes_to_a_queue_once(home, client):
    body = {"kind": "up", "file": UP_FILE, "tickers": []}
    first = client.post("/api/queues/trade", json=body).json()
    assert first["added"] == ["NVDA", "MSFT"]
    again = client.post("/api/queues/trade", json=body).json()
    assert again["added"] == [] and again["already_queued"] == ["NVDA", "MSFT"]
    items = client.get("/api/queues").json()["trade"]
    assert {i["ticker"] for i in items} == {"NVDA", "MSFT"}
    nvda = next(i for i in items if i["ticker"] == "NVDA")
    assert nvda["metrics"]["close"] == 234.0 and nvda["side"] == "long"  # read from the file, not the browser


@pytest.mark.unit
def test_research_queue_shows_route_status(home, client):
    client.post("/api/queues/research", json={"kind": "up", "file": UP_FILE, "tickers": ["NVDA", "MSFT"]})
    status = {i["ticker"]: i["route_status"] for i in client.get("/api/queues").json()["research"]}
    assert status == {"NVDA": "BLOCKED", "MSFT": "NOT_ROUTED"}


@pytest.mark.unit
def test_strategy_queue_checks_the_regime(home, client):
    client.post("/api/queues/strategy", json={"kind": "up", "file": UP_FILE, "tickers": ["NVDA"]})
    client.post("/api/queues/strategy", json={"kind": "long", "file": LONG_FILE, "tickers": ["LOGI"]})
    verdicts = {i["ticker"]: i["verdict"] for i in client.get("/api/queues").json()["strategy"]}
    assert verdicts == {"NVDA": "BLOCKED", "LOGI": "ALLOWED"}  # regime SIDE: trend long blocked, sideways long allowed


@pytest.mark.unit
def test_a_queued_item_can_be_removed(home, client):
    client.post("/api/queues/trade", json={"kind": "long", "file": LONG_FILE, "tickers": ["LOGI"]})
    item = client.get("/api/queues").json()["trade"][0]
    assert client.delete(f"/api/queues/trade/{item['id']}").json() == {"removed": True}
    assert client.get("/api/queues").json()["trade"] == []


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    {"kind": "up", "file": UP_FILE, "tickers": ["SPY"]},  # rejected, not a member
    {"kind": "down", "file": UP_FILE, "tickers": []},  # wrong kind for the file
    {"kind": "up", "file": "..\\..\\.env", "tickers": []},
    {"kind": "orders", "file": UP_FILE, "tickers": []},
])
def test_bad_sends_are_refused(home, client, body):
    assert client.post("/api/queues/trade", json=body).status_code == 400
    assert client.post("/api/queues/nowhere", json={"kind": "up", "file": UP_FILE}).status_code == 400


@pytest.mark.unit
def test_trash_removes_a_ticker_from_the_basket_file(home, client):
    assert client.post("/api/scans/trash", json={"kind": "up", "file": UP_FILE, "ticker": "NVDA"}).status_code == 200
    data = json.loads((home / "baskets" / UP_FILE).read_text())
    assert [m["ticker"] for m in data["members"]] == ["MSFT"]


@pytest.mark.unit
def test_trash_marks_a_channel_member_not_passed(home, client):
    client.post("/api/scans/trash", json={"kind": "long", "file": LONG_FILE, "ticker": "LOGI"})
    data = json.loads((home / "scanbot" / LONG_FILE).read_text())
    assert data["members"] == ["IDXX"]
    assert data["calls"]["LOGI"]["passed"] is False and "removed_by_operator" in data["calls"]["LOGI"]["reasons"]


@pytest.mark.unit
def test_trash_deletes_the_whole_file(home, client):
    assert client.post("/api/scans/trash", json={"kind": "long", "file": LONG_FILE}).json() == {"deleted": LONG_FILE}
    assert not (home / "scanbot" / LONG_FILE).exists()
    assert client.post("/api/scans/trash", json={"kind": "long", "file": LONG_FILE}).status_code == 400


@pytest.mark.unit
def test_trash_refuses_files_outside_the_scan_folders(home, client):
    _write(home / "settings.json", {})
    assert client.post("/api/scans/trash", json={"kind": "up", "file": "settings.json"}).status_code == 400
    assert (home / "settings.json").exists()
