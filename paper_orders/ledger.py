"""The local order log: one JSON line per entry, exit, guard decision and flatten action, by month.

Lines are appended to <data home>/orders/ledger_YYYY-MM.jsonl (``extensions.tradebot.store.data_home``) under a
file lock, so a scheduled send and a send from the UI never interleave a line. The log records what this
machine asked for and what the paper account answered at that moment; a fill that lands later is logged with
the next sender run's exit and is always visible to ``paper_orders.reconcile``, which reads the account itself.
The account's own order history stays the source of truth.

Each line: at, mode (tradebot, daily, manual, flatten), kind (entry, exit, guard, flatten), symbol,
client_order_id, item_id, action, status, qty, filled_qty, filled_avg_price, stop, reason. Nothing here imports
Alpaca.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock, Timeout

from extensions.tradebot import store

LEDGER_VERSION = 1
LOCK_TIMEOUT = 30.0
FIELDS = ("at", "mode", "kind", "symbol", "client_order_id", "item_id", "action", "status", "qty", "filled_qty",
          "filled_avg_price", "stop", "reason")
ITEM_ID_PREFIX = "tb-"  # tb-<item>-<attempt>-...: the Tradebot item an order belongs to


class LedgerError(RuntimeError):
    """The ledger cannot be written or read."""


def ledger_dir() -> Path:
    return store.data_home() / "orders"


def ledger_path(month: str) -> Path:
    return ledger_dir() / f"ledger_{month}.jsonl"


def item_id_of(coid: str | None) -> str | None:
    """The Tradebot queue item an order ID belongs to, or None for a daily or manual order."""
    if coid and coid.startswith(ITEM_ID_PREFIX):
        parts = coid.split("-")
        return parts[1] if len(parts) > 2 else None
    return None


def line(mode: str, kind: str, symbol: str | None, *, client_order_id: str | None = None, action: str | None = None,
         order: dict | None = None, stop: float | None = None, reason: str | None = None,
         at: datetime | None = None) -> dict:
    """One ledger line, with the order's status and fill read from the account's answer."""
    order = order or {}
    return {"ledger_version": LEDGER_VERSION, "at": (at or datetime.now(UTC)).isoformat(timespec="seconds"),
            "mode": mode, "kind": kind, "symbol": symbol,
            "client_order_id": client_order_id or order.get("client_order_id"),
            "item_id": item_id_of(client_order_id or order.get("client_order_id")), "action": action,
            "status": order.get("status"), "qty": order.get("qty"), "filled_qty": order.get("filled_qty"),
            "filled_avg_price": order.get("filled_avg_price"), "stop": stop, "reason": reason}


def append(lines: Iterable[dict]) -> Path | None:
    """Append the lines to this month's file under the lock. Returns the file, or None when there was nothing."""
    rows = list(lines)
    if not rows:
        return None
    try:
        store.assert_not_synced()
    except store.StoreError as exc:
        raise LedgerError(str(exc)) from exc
    month = rows[0]["at"][:7]
    path = ledger_path(month)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, separators=(",", ":"), default=str) + "\n" for r in rows)
    try:
        with FileLock(str(path) + ".lock", timeout=LOCK_TIMEOUT), path.open("a", encoding="utf-8") as fh:
            fh.write(text)
    except Timeout as exc:
        raise LedgerError(f"{path} stayed locked for {LOCK_TIMEOUT:g}s") from exc
    except OSError as exc:
        raise LedgerError(f"cannot write {path}: {exc}") from exc
    return path


def read(month: str | None = None) -> list[dict]:
    """The lines of one month (default: this one), oldest first. A line that is not JSON is an error."""
    month = month or datetime.now(UTC).strftime("%Y-%m")
    path = ledger_path(month)
    if not path.exists():
        return []
    out = []
    for n, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not text.strip():
            continue
        try:
            out.append(json.loads(text))
        except ValueError as exc:
            raise LedgerError(f"{path} line {n} is not JSON: {exc}") from exc
    return out


# --- the sender's hook ------------------------------------------------------------------

def sender_lines(mode: str, entries: list, exits: list, decision) -> list[dict]:
    """Lines for one sender --send: each entry and exit outcome, and each name the guard held back."""
    at = datetime.now(UTC)
    rows = [line(mode, "entry", o.symbol, client_order_id=o.client_order_id, action=o.action, order=o.order,
                 reason=o.error, at=at) for o in entries]
    for o in exits:
        row = line(mode, "exit", o.symbol, client_order_id=o.client_order_id, action=o.action, order=o.order,
                   stop=o.stop, reason=o.error, at=at)
        if o.fill is not None:  # the entry's fill the exit was measured from
            row.update(entry_filled_qty=o.fill.qty, entry_fill_price=o.fill.price)
        rows.append(row)
    if decision is not None:
        for symbol, why in decision.skipped.items():
            rows.append(line(mode, "guard", symbol, action="blocked" if decision.blocked else "skipped",
                             reason=why, at=at))
    return rows


def record_send(mode: str, entries: list, exits: list, decision) -> None:
    """paper_orders.sender's on_outcomes hook. A ledger that cannot be written warns; it never changes what was
    sent or the sender's exit code."""
    try:
        path = append(sender_lines(mode, entries, exits, decision))
    except LedgerError as exc:
        print(f"warning: the order ledger was not written: {exc}", file=sys.stderr)
        return
    if path is not None:
        print(f"ledger: {path}")
