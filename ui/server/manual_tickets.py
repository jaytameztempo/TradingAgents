"""Manual paper tickets: the Trade page's order entry writes them, and paper_orders.sender sends them.

This module only writes, lists and deletes files. It places no order and imports nothing from
alpaca.trading: a confirmed Send on the Trade page starts ``python -m paper_orders.sender --manual <name>
--send`` as a job (ui/server/routes/trade.py), and the sender applies its own checks (paper key, paper URL,
trading window, one client order ID per order) before anything reaches Alpaca.

Each ticket is one file in <data home>/tickets/manual/, named manual_ticket_<as_of>_<SYMBOL>_<id>.json, in
the format paper_orders.sender.load_manual reads. Symbol, side and date come from the queued row (which the
server built from the scan file), never from the browser; only the prices and the quantity are typed in.
Sends started from the page are logged in sent.json beside the tickets, and a sent ticket cannot be deleted.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from extensions.market_data.alpaca_bars import validate_ticker
from extensions.status.paper_tickets import stop_for_fill
from ui.server import paths, queues

MANUAL_VERSION = 1  # paper_orders.sender.MANUAL_VERSION
ORDER_SIDE = {"long": "buy", "short": "sell"}
MAX_QUANTITY = 100_000
NAME = re.compile(r"^manual_ticket_(\d{4}-\d{2}-\d{2})_([A-Z]{1,5}(?:[.-][A-Z]{1,2})?)_([0-9a-f]{12})\.json$")
_lock = threading.Lock()


class TicketError(ValueError):
    """Bad input: an unknown queue row or ticket, prices on the wrong sides, or a sent ticket to delete."""


def manual_dir() -> Path:
    return paths.tickets_dir() / "manual"


def _sent_path() -> Path:
    return manual_dir() / "sent.json"


def _read_sent() -> dict[str, list[dict]]:
    try:
        data = json.loads(_sent_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _cents(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
        raise TicketError(f"{name} must be a price above zero")
    return round(float(value), 2)


def create(item_id: str, entry: float, stop: float, target: float, quantity: int) -> dict:
    """Write one ticket for a row of the trade queue. Nothing is sent."""
    item = next((i for i in queues._read_queues()["trade"] if i.get("id") == item_id), None)
    if item is None:
        raise TicketError("that row is no longer in the trade queue")
    symbol, side = validate_ticker(item["ticker"]), item.get("side")
    if side not in ORDER_SIDE:
        raise TicketError(f"queued row has side {side!r}")
    try:
        as_of = date.fromisoformat(item.get("as_of") or "").isoformat()
    except ValueError:
        raise TicketError(f"queued row has no scan date: {item.get('as_of')!r}") from None
    entry, stop, target = _cents(entry, "entry"), _cents(stop, "stop"), _cents(target, "target")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_QUANTITY:
        raise TicketError(f"quantity must be a whole number from 1 to {MAX_QUANTITY:,}")
    sign = 1 if side == "long" else -1
    if not sign * (entry - stop) > 0:
        raise TicketError(f"a {side} stop must be {'below' if side == 'long' else 'above'} the entry")
    if not sign * (target - entry) > 0:
        raise TicketError(f"a {side} target must be {'above' if side == 'long' else 'below'} the entry")
    stop_offset = round(sign * (entry - stop), 2)
    if stop_for_fill(side, stop_offset, entry) != stop:  # the sender re-checks this
        raise TicketError(f"stop {stop} is not {stop_offset} from entry {entry}")

    ticket_id = uuid.uuid4().hex[:12]
    name = f"manual_ticket_{as_of}_{symbol}_{ticket_id}.json"
    data = {
        "kind": "manual", "manual_version": MANUAL_VERSION, "id": ticket_id, "as_of": as_of,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"), "paper": True, "orders_placed": False,
        "source": {"queue_item": item["id"], "scan": item.get("scan"), "scan_file": item.get("scan_file"),
                   "kind": item.get("kind")},
        "tickets": [{"symbol": symbol, "side": side, "order_side": ORDER_SIDE[side], "quantity": quantity,
                     "entry": entry, "stop": stop, "stop_offset": stop_offset, "target": target, "paper": True}],
    }
    queues._write_json(manual_dir() / name, data)
    return _row(name, data, [])


def _row(name: str, data: dict, sends: list[dict]) -> dict:
    t = data["tickets"][0]
    risk = t["stop_offset"] * t["quantity"]
    reward = abs(t["target"] - t["entry"])
    return {
        "name": name, "id": data["id"], "as_of": data["as_of"], "created_at": data.get("created_at"),
        "source": data.get("source") or {}, **{k: t[k] for k in
        ("symbol", "side", "order_side", "quantity", "entry", "stop", "stop_offset", "target")},
        "risk_dollars": round(risk, 2), "notional": round(t["entry"] * t["quantity"], 2),
        "reward_risk": round(reward / t["stop_offset"], 2), "sends": sends,
    }


def list_all() -> list[dict]:
    """Every manual ticket, newest first, with the sends started from the page."""
    folder = manual_dir()
    if not folder.is_dir():
        return []
    sent = _read_sent()
    rows = []
    for path in folder.iterdir():
        if not NAME.fullmatch(path.name):
            continue
        try:
            rows.append(_row(path.name, json.loads(path.read_text(encoding="utf-8")), sent.get(path.name, [])))
        except (OSError, ValueError, KeyError, TypeError, IndexError, ZeroDivisionError) as exc:
            rows.append({"name": path.name, "error": f"cannot read: {exc}", "sends": sent.get(path.name, [])})
    return sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)


def check_name(name: str) -> str:
    """name, if it is a manual ticket file that exists; the sender re-reads and re-checks it."""
    if not NAME.fullmatch(name) or not (manual_dir() / name).is_file():
        raise TicketError(f"no such manual ticket: {name}")
    return name


def record_send(name: str, job_id: str) -> None:
    with _lock:
        sent = _read_sent()
        sent.setdefault(name, []).append({"job_id": job_id, "at": datetime.now(UTC).isoformat(timespec="seconds")})
        queues._write_json(_sent_path(), sent)


def delete(name: str) -> None:
    """Delete a ticket that was never sent. A sent one stays as the record of its order IDs."""
    check_name(name)
    with _lock:
        if _read_sent().get(name):
            raise TicketError(f"{name} was sent to paper; it stays as the record of its orders")
        (manual_dir() / name).unlink()
