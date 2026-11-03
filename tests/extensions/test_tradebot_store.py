"""The Tradebot queue store: item life cycle, duplicates, expiry, and the cross-process lock."""

import json
import subprocess
import sys
import textwrap
import threading
from datetime import date
from pathlib import Path

import pytest

from extensions.tradebot import store

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(store.HOME_ENV, str(tmp_path))
    return tmp_path


def _item(ticker="NVDA", as_of="2026-10-09", bot="UP", side="long", kind="up_cat", source="auto"):
    return store.new_item(ticker=ticker, as_of=as_of, kind=kind, scan="Category upward (Category 1)",
                          scan_file=f"scan-up-category-1_{as_of}_20261010T000000Z.json", side=side,
                          needs_regime=bot, metrics={"close": 100.0}, source=source)


def _children(home: Path, code: str, n: int) -> list[subprocess.CompletedProcess]:
    """Run n copies of code at once, each in its own Python process, all sharing home."""
    script = textwrap.dedent(code)
    env = {**__import__("os").environ, store.HOME_ENV: str(home)}
    procs = [subprocess.Popen([sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(n)]
    out = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=120)
        out.append(subprocess.CompletedProcess(p.args, p.returncode, stdout, stderr))
    return out


@pytest.mark.unit
def test_a_new_trade_item_waits_and_expires_after_ten_sessions(home):
    item = _item(as_of="2026-10-09")  # a Friday
    assert item["status"] == "waiting" and item["bot"] == "UP" and item["source"] == "auto"
    assert item["checks"] == [] and item["ticket"] is None and item["last_guard"] is None
    assert item["expires_after"] == "2026-10-23"  # ten sessions: Oct 12-16 and Oct 19-23


@pytest.mark.unit
def test_expiry_skips_market_holidays():
    # Thanksgiving 2026-11-26 is closed: ten sessions after Nov 20 run to Dec 7, not Dec 4
    assert store.expires_after(date(2026, 11, 20)) == date(2026, 12, 7)


@pytest.mark.unit
def test_a_ticker_is_queued_once_per_bot_while_active(home):
    assert store.enqueue("trade", [_item()])["added"] == ["NVDA"]
    again = store.enqueue("trade", [_item(as_of="2026-10-12")])
    assert again == {"queue": "trade", "added": [], "already_queued": ["NVDA"]}
    rows = store.read()["trade"]
    assert len(rows) == 1 and rows[0]["as_of"] == "2026-10-12"  # refreshed with the newer scan
    assert store.enqueue("trade", [_item(bot="SIDE", kind="side_long_cat")])["added"] == ["NVDA"]  # another bot


@pytest.mark.unit
def test_a_finished_item_does_not_block_a_new_one(home):
    store.enqueue("trade", [_item()])
    with store.transaction() as q:
        store.set_status(q["trade"][0], "expired", "past 2026-10-23")
    assert store.enqueue("trade", [_item(as_of="2026-10-26")])["added"] == ["NVDA"]
    assert [r["status"] for r in store.read()["trade"]] == ["expired", "waiting"]


@pytest.mark.unit
def test_checks_keep_the_last_twenty(home):
    store.enqueue("trade", [_item()])
    with store.transaction() as q:
        for n in range(25):
            store.add_check(q["trade"][0], {"stage": "eod", "n": n})
    checks = store.read()["trade"][0]["checks"]
    assert len(checks) == store.MAX_CHECKS and checks[0]["n"] == 5 and checks[-1]["n"] == 24


@pytest.mark.unit
def test_an_unknown_status_is_refused(home):
    with pytest.raises(store.StoreError):
        store.set_status({}, "maybe")


@pytest.mark.unit
def test_a_ticketed_item_is_kept_as_removed(home):
    plain, ticketed = _item("AAPL"), _item("MSFT")
    store.enqueue("trade", [plain, ticketed])
    store.update_item(ticketed["id"], status="ticketed", ticket={"file": "t.json"})
    assert store.remove("trade", plain["id"]) and store.remove("trade", ticketed["id"])
    rows = store.read()["trade"]
    assert [(r["ticker"], r["status"]) for r in rows] == [("MSFT", "removed")]


@pytest.mark.unit
def test_an_old_item_reads_with_the_tradebot_fields(home):
    (home / "queues.json").write_text(json.dumps({"trade": [{
        "id": "abc", "ticker": "LOGI", "as_of": "2026-10-09", "kind": "long", "scan": "Long sideways channel",
        "scan_file": "long_channel_x.json", "side": "long", "needs_regime": "SIDE", "metrics": {}}]}), encoding="utf-8")
    item = store.read()["trade"][0]
    assert (item["status"], item["bot"], item["source"], item["expires_after"]) == (
        "waiting", "SIDE", "manual", "2026-10-23")


@pytest.mark.unit
def test_a_broken_queue_file_is_an_error_not_an_empty_queue(home):
    (home / "queues.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(store.StoreError, match="not valid JSON"):
        store.read()


@pytest.mark.unit
def test_an_exception_in_a_transaction_writes_nothing(home):
    store.enqueue("trade", [_item()])
    with pytest.raises(RuntimeError), store.transaction() as q:
        store.set_status(q["trade"][0], "blocked", "x")
        raise RuntimeError("stop")
    assert store.read()["trade"][0]["status"] == "waiting"


@pytest.mark.unit
def test_two_processes_cannot_both_enqueue_the_same_item(home):
    results = _children(home, """
        from extensions.tradebot import store
        for _ in range(15):
            store.enqueue("trade", [store.new_item(ticker="NVDA", as_of="2026-10-09", kind="up_cat", scan="s",
                                                   scan_file="f.json", side="long", needs_regime="UP", metrics={},
                                                   source="auto")])
    """, n=4)
    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    assert [r["ticker"] for r in store.read()["trade"]] == ["NVDA"]


@pytest.mark.unit
def test_two_processes_updating_one_item_lose_no_update(home):
    item = _item()
    store.enqueue("trade", [item])
    results = _children(home, f"""
        from extensions.tradebot import store
        for _ in range(25):
            with store.transaction() as q:
                it = store.find(q, {item['id']!r})
                it["counter"] = it.get("counter", 0) + 1
    """, n=3)
    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    assert store.read()["trade"][0]["counter"] == 75


@pytest.mark.unit
def test_a_transaction_holder_blocks_a_second_writer(home):
    store.enqueue("trade", [_item()])
    held, release = threading.Event(), threading.Event()

    def hold():
        with store.transaction():
            held.set()
            release.wait(30)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert held.wait(10)
        [result] = _children(home, """
            import sys
            from extensions.tradebot import store
            store.LOCK_TIMEOUT = 0.5
            try:
                store.enqueue("trade", [])
            except store.StoreError as exc:
                print("blocked:", exc)
                sys.exit(0)
            sys.exit(1)
        """, n=1)
        assert result.returncode == 0 and "stayed locked" in result.stdout, result.stderr
    finally:
        release.set()
        t.join()
