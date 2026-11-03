"""The work queues file, ~/.tradingagents/queues.json, behind one cross-process lock.

The UI server (its request threads and scheduler) and the Tradebot jobs (separate processes) all read and write
this file, so every read and every read-modify-write takes ``queues.json.lock`` (``filelock``) and every write
ends in an atomic ``os.replace``. ``transaction()`` holds the lock across a whole run, so a ticketing run that
reads the queue, sizes against it and marks items ticketed cannot interleave with another.

Three queues live here: research, strategy and trade. The trade queue is the Tradebot queue. Each trade item
moves through STATUSES:

- waiting: queued; the daily triggers have not held yet
- armed: the daily triggers held; waiting for the 4-hour and 1-hour squeeze fire
- triggered: every enabled trigger held; ready for a ticket
- ticketed: a ticket file names it; not sent yet
- sent: its entry went to the paper account
- expired, blocked, removed: out of the queue (kept for the record, never re-checked)

A ticker is queued once per bot while it is ACTIVE; queuing it again refreshes its scan numbers instead. Items
written before the Tradebot existed have no status: they read as waiting, manual, expiring EXPIRE_SESSIONS
sessions after their scan date.

The home folder is TRADINGAGENTS_UI_HOME when set (the UI's tests and jobs share it), else ~/.tradingagents.
Nothing here imports Alpaca.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from filelock import FileLock, Timeout

from paper_orders import market_calendar

HOME_ENV = "TRADINGAGENTS_UI_HOME"  # as ui.server.paths.HOME_ENV
QUEUES = ("research", "strategy", "trade")
STATUSES = ("waiting", "armed", "triggered", "ticketed", "sent", "expired", "blocked", "removed")
ACTIVE = frozenset({"waiting", "armed", "triggered", "ticketed"})  # a sent name is guarded by its position
CHECKABLE = frozenset({"waiting", "armed"})
SOURCES = ("auto", "manual")
EXPIRE_SESSIONS = 10
MAX_CHECKS = 20
LOCK_TIMEOUT = 30.0  # seconds; a run that cannot get the lock in this time fails instead of waiting forever
REPLACE_TRIES = 10  # Windows refuses os.replace while another program has the file open for a moment


class StoreError(RuntimeError):
    """The queue file is locked too long, unreadable, or an item or status is not one the store knows."""


def data_home() -> Path:
    return Path(os.environ.get(HOME_ENV) or Path.home() / ".tradingagents")


def queue_path() -> Path:
    return data_home() / "queues.json"


SYNC_ENVS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


def synced_folder(path: Path | None = None) -> str | None:
    """The OneDrive folder that holds path (default: the data home), or None. A synced folder can lock or roll
    back the queue, its lock file, the ledger and the risk state, so the Tradebot refuses to run inside one."""
    target = (path or data_home()).resolve()
    for name in SYNC_ENVS:
        root = os.environ.get(name)
        if root:
            try:
                target.relative_to(Path(root).resolve())
            except ValueError:
                continue
            return f"%{name}% ({root})"
    return None


def assert_not_synced(path: Path | None = None) -> None:
    where = synced_folder(path)
    if where:
        raise StoreError(f"the data folder {(path or data_home())} is inside {where}; move it out of OneDrive "
                         f"(or set {HOME_ENV}) so sync never touches the queue, ledger or lock files")


def _lock() -> FileLock:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path) + ".lock", timeout=LOCK_TIMEOUT)


def expires_after(as_of: date, sessions: int = EXPIRE_SESSIONS) -> date:
    """The trading session `sessions` sessions after as_of: the last session the item may still trigger."""
    d, left = as_of, sessions
    while left > 0:
        d += timedelta(days=1)
        if market_calendar.is_trading_day(d):
            left -= 1
    return d


def _normalize(queue: str, item: dict) -> dict:
    """Fill the Tradebot fields an older trade item lacks; other queues are returned as they are."""
    if queue != "trade":
        return item
    item.setdefault("status", "waiting")
    item.setdefault("bot", item.get("needs_regime"))
    item.setdefault("source", "manual")
    if not item.get("expires_after") and item.get("as_of"):
        try:
            item["expires_after"] = expires_after(date.fromisoformat(item["as_of"])).isoformat()
        except ValueError:
            item["expires_after"] = None
    item.setdefault("checks", [])
    item.setdefault("ticket", None)
    item.setdefault("last_guard", None)
    item.setdefault("status_reason", None)
    return item


def _read(path: Path) -> dict[str, list[dict]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except OSError as exc:
        raise StoreError(f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise StoreError(f"{path} is not valid JSON ({exc}); fix or move it before queuing more") from exc
    if not isinstance(data, dict):
        raise StoreError(f"{path} does not hold the queues")
    return {q: [_normalize(q, i) for i in (data.get(q) or []) if isinstance(i, dict)] for q in QUEUES}


def _write(path: Path, queues: dict[str, list[dict]]) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(queues, indent=2), encoding="utf-8")
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (attempt + 1))


@contextmanager
def transaction() -> Iterator[dict[str, list[dict]]]:
    """The queues, locked until the block ends, then written back. An exception in the block writes nothing."""
    try:
        with _lock():
            path = queue_path()
            queues = _read(path)
            yield queues
            _write(path, queues)
    except Timeout as exc:
        raise StoreError(f"{queue_path()} stayed locked for {LOCK_TIMEOUT:g}s; is another run stuck?") from exc


def read() -> dict[str, list[dict]]:
    """A consistent copy of every queue, read under the lock."""
    try:
        with _lock():
            return _read(queue_path())
    except Timeout as exc:
        raise StoreError(f"{queue_path()} stayed locked for {LOCK_TIMEOUT:g}s; is another run stuck?") from exc


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_item(*, ticker: str, as_of: str | None, kind: str, scan: str, scan_file: str, side: str,
             needs_regime: str, metrics: dict, queue: str = "trade", source: str = "manual",
             expire_sessions: int = EXPIRE_SESSIONS) -> dict:
    """One queue row. Trade rows carry the Tradebot fields; the others keep the plain row."""
    item = {"id": uuid.uuid4().hex[:12], "ticker": ticker, "as_of": as_of, "kind": kind, "scan": scan,
            "scan_file": scan_file, "side": side, "needs_regime": needs_regime, "metrics": metrics,
            "added_at": now_iso()}
    if queue == "trade":
        if source not in SOURCES:
            raise StoreError(f"unknown source {source!r}")
        item.update(status="waiting", status_reason=None, bot=needs_regime, source=source,
                    expires_after=expires_after(date.fromisoformat(as_of), expire_sessions).isoformat()
                    if as_of else None,
                    checks=[], ticket=None, last_guard=None)
    return item


def _same(queue: str, a: dict, b: dict) -> bool:
    """Whether b duplicates a: in the trade queue, the same ticker and bot while a is active; elsewhere the same
    ticker, scan date and scan kind."""
    if queue == "trade":
        return a.get("status") in ACTIVE and a["ticker"] == b["ticker"] and a.get("bot") == b.get("bot")
    return (a["ticker"], a.get("as_of"), a.get("kind")) == (b["ticker"], b.get("as_of"), b.get("kind"))


def enqueue(queue: str, items: list[dict]) -> dict:
    """Add the items not already queued. A trade duplicate gets the newer scan's numbers instead (and keeps its
    place in the queue and its status). Returns {"added": [...], "already_queued": [...]} by ticker."""
    if queue not in QUEUES:
        raise StoreError(f"unknown queue {queue!r}")
    added, already = [], []
    with transaction() as queues:
        rows = queues[queue]
        for item in items:
            have = next((r for r in rows if _same(queue, r, item)), None)
            if have is None:
                rows.append(item)
                added.append(item["ticker"])
                continue
            already.append(item["ticker"])
            if queue == "trade" and (item.get("as_of") or "") > (have.get("as_of") or ""):
                have.update(as_of=item["as_of"], kind=item["kind"], scan=item["scan"],
                            scan_file=item["scan_file"], metrics=item["metrics"], refreshed_at=now_iso())
    return {"queue": queue, "added": added, "already_queued": already}


def remove(queue: str, item_id: str) -> bool:
    """Drop a row. A trade row that was ever ticketed or sent is kept as removed, for the record."""
    if queue not in QUEUES:
        raise StoreError(f"unknown queue {queue!r}")
    with transaction() as queues:
        rows = queues[queue]
        item = next((i for i in rows if i.get("id") == item_id), None)
        if item is None:
            return False
        if queue == "trade" and (item.get("ticket") or item.get("status") == "sent"):
            item.update(status="removed", status_reason="removed by the operator", status_at=now_iso())
        else:
            rows.remove(item)
        return True


def find(queues: dict[str, list[dict]], item_id: str, queue: str = "trade") -> dict:
    item = next((i for i in queues[queue] if i.get("id") == item_id), None)
    if item is None:
        raise StoreError(f"no {queue} queue item {item_id!r}")
    return item


def set_status(item: dict, status: str, reason: str | None = None) -> None:
    """Move a trade item (inside a transaction) to status, with the reason."""
    if status not in STATUSES:
        raise StoreError(f"unknown status {status!r}")
    item.update(status=status, status_reason=reason, status_at=now_iso())


def add_check(item: dict, check: dict) -> None:
    """Record one trigger check on a trade item (inside a transaction), keeping the last MAX_CHECKS."""
    item["checks"] = [*(item.get("checks") or []), {"at": now_iso(), **check}][-MAX_CHECKS:]


def update_item(item_id: str, **fields) -> dict:
    """Set fields on one trade item under the lock and return it."""
    with transaction() as queues:
        item = find(queues, item_id)
        item.update(fields)
        return dict(item)
