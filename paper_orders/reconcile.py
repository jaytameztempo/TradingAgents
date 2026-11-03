"""Check the paper account against the tickets and the Tradebot queue. Read-only: it never places or changes an order.

Run from the TradingAgents folder:

    python -m paper_orders.reconcile               # the last 10 days
    python -m paper_orders.reconcile --days 5 --json

It reads the account with ``paper_orders.snapshot.PaperReader`` (HTTPS GET only, paper keys only; Alpaca's
trading client is never loaded): open positions, open orders, and every order of the last --days days. It
compares them with every ticket file (daily ``paper_tickets``, manual and Tradebot tickets, under <data
home>/tickets; a ticket counts when its orders are among the account's) and the Tradebot queue, and reports:

- entry_filled_no_exit: a ticket's entry filled, its position is open, and its OCO exit is not on the account
  (run the sender again),
- position_no_stop: an open position with no open stop order (the guard blocks every entry while this stands),
- exit_qty_mismatch: the open exit orders for a position cover a different quantity than the position holds,
- orphan_position: an open position no ticket on file accounts for with a filled entry,
- stale_open_entry: an entry (not an exit) still open after the close, or from an earlier session,
- rejected: an order with a pt-, mt- or tb- client order ID the account rejected,
- queue_*: a Tradebot item whose status disagrees with the account (sent but no entry; ticketed but the entry
  is already there; exit done but no exit order),
- history_truncated: the account returned the 500-order page limit, so older orders may be missing.

The report is written to <data home>/status/reconcile_<stamp>.json. Exit code 0 when clean, 3 when anything is
reported, 1 when the account cannot be read, 2 for bad input.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from extensions.tradebot import store
from paper_orders.ledger import item_id_of
from paper_orders.snapshot import NotPaperKey, ReadFailed, paper_reader

EXIT_OK, EXIT_READ_FAILED, EXIT_BAD_INPUT, EXIT_PROBLEMS = 0, 1, 2, 3
REPORT_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[1]
MARKET_TZ = ZoneInfo("America/New_York")
SESSION_CLOSE = time(16, 0)
PAGE_LIMIT = 500
OUR_PREFIXES = ("pt-", "mt-", "tb-")
STOP_TYPES = {"stop", "stop_limit", "trailing_stop"}
FINAL = {"filled", "rejected", "canceled", "expired", "done_for_day", "replaced"}
ORDER_SIDE = {"long": "buy", "short": "sell"}


@dataclass
class Problem:
    type: str
    symbol: str | None
    detail: str
    client_order_id: str | None = None
    item_id: str | None = None

    def __post_init__(self) -> None:
        if self.item_id is None:
            self.item_id = item_id_of(self.client_order_id)  # a tb- order names its Tradebot item


@dataclass
class Report:
    created_at: str
    after: str
    window_days: int
    counts: dict = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    report_version: int = REPORT_VERSION

    @property
    def clean(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {**asdict(self), "clean": self.clean,
                "by_type": {t: sum(1 for p in self.problems if p.type == t)
                            for t in sorted({p.type for p in self.problems})}}


@dataclass(frozen=True)
class Expected:
    """One ticket's orders, as the sender names them."""
    symbol: str
    side: str
    quantity: int
    entry_id: str
    exit_id: str
    source: str  # ticket file name
    as_of: str


# --- tickets on disk -------------------------------------------------------------------

def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def expected_orders(since: date | None = None, tickets_dir: Path | None = None) -> list[Expected]:
    """Every ticket (dated on or after since, when given), with its entry and exit client order IDs. Reconcile
    reads them all: a Tradebot ticket can be sent days after its date, and only the tickets whose orders are on
    the account take part in any check."""
    base = tickets_dir or store.data_home() / "tickets"
    out: list[Expected] = []

    def add(t: dict, prefix: str, source: str, as_of: str) -> None:
        side = t.get("side")
        if side not in ORDER_SIDE or not t.get("symbol"):
            return
        out.append(Expected(t["symbol"], side, int(t.get("quantity") or 0),
                            f"{prefix}-{t['symbol']}-{ORDER_SIDE[side]}", f"{prefix}-{t['symbol']}-exit", source, as_of))

    def recent(as_of) -> bool:
        if since is None:
            return True
        try:
            return date.fromisoformat(as_of or "") >= since
        except ValueError:
            return False

    for path in sorted(base.glob("paper_tickets_*.json")):
        data = _read(path)
        if recent(data.get("as_of")):
            for t in data.get("tickets") or []:
                add(t, f"pt-{data['as_of']}", path.name, data["as_of"])
    for path in sorted((base / "manual").glob("manual_ticket_*.json")):
        data = _read(path)
        if recent(data.get("as_of")):
            for t in data.get("tickets") or []:
                add(t, f"mt-{data['as_of']}-{data.get('id')}", path.name, data["as_of"])
    for path in sorted((base / "tradebot").glob("tradebot_tickets_*.json")):
        data = _read(path)
        if recent(data.get("as_of")):
            for t in data.get("tickets") or []:
                if t.get("id_prefix"):
                    add(t, t["id_prefix"], path.name, data["as_of"])
    return out


# --- the checks --------------------------------------------------------------------------

def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _when(order: dict) -> datetime | None:
    raw = order.get("submitted_at") or order.get("created_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _flat(orders: list[dict]) -> list[dict]:
    out = []
    for o in orders:
        out.append(o)
        out.extend(o.get("legs") or [])
    return out


def _is_entry(o: dict) -> bool:
    if (o.get("order_class") or "simple") != "simple" or o.get("legs"):
        return False
    if (o.get("client_order_id") or "").endswith("-exit"):
        return False
    return (o.get("type") or o.get("order_type")) not in STOP_TYPES


def check(positions: list[dict], open_orders: list[dict], recent: list[dict], expected: list[Expected],
          queue: list[dict], now: datetime) -> list[Problem]:
    problems: list[Problem] = []
    by_id = {o.get("client_order_id"): o for o in [*recent, *open_orders] if o.get("client_order_id")}
    held = {p.get("symbol"): p for p in positions}
    open_flat = _flat(open_orders)

    # positions: a stop, the exit quantity, and a ticket behind each
    filled_symbols = {e.symbol for e in expected if _num((by_id.get(e.entry_id) or {}).get("filled_qty")) > 0}
    for symbol, p in held.items():
        qty = abs(_num(p.get("qty")))
        stops = [o for o in open_flat if o.get("symbol") == symbol and (o.get("type") or o.get("order_type")) in STOP_TYPES]
        if not stops:
            problems.append(Problem("position_no_stop", symbol, f"{qty:g} shares open with no open stop order"))
        exits = [o for o in open_orders if o.get("symbol") == symbol and not _is_entry(o)]
        covered = sum(_num(o.get("qty")) - _num(o.get("filled_qty")) for o in exits)
        if exits and abs(covered - qty) > 1e-9:
            problems.append(Problem("exit_qty_mismatch", symbol,
                                    f"open exits cover {covered:g} shares; the position holds {qty:g}",
                                    exits[0].get("client_order_id")))
        if symbol not in filled_symbols:
            problems.append(Problem("orphan_position", symbol,
                                    f"{qty:g} shares open; no ticket on file has a filled entry for it"))

    # each ticket: a filled entry with its position still open needs its exit
    for e in expected:
        entry = by_id.get(e.entry_id)
        if entry is None or _num(entry.get("filled_qty")) <= 0 or e.symbol not in held:
            continue
        if entry.get("status") not in FINAL:
            continue  # still filling: the sender places the exit once it is done
        if e.exit_id not in by_id:
            problems.append(Problem("entry_filled_no_exit", e.symbol,
                                    f"{e.entry_id} filled {_num(entry.get('filled_qty')):g} at "
                                    f"{entry.get('filled_avg_price')}; no exit {e.exit_id} (run the sender again)",
                                    e.entry_id))

    # open entries after the close, or from an earlier session
    local = now.astimezone(MARKET_TZ)
    for o in open_orders:
        if not _is_entry(o):
            continue
        at = _when(o)
        day = at.astimezone(MARKET_TZ).date() if at else None
        if day is None or day < local.date() or local.time() >= SESSION_CLOSE:
            problems.append(Problem("stale_open_entry", o.get("symbol"),
                                    f"entry {o.get('client_order_id') or o.get('id')} is still {o.get('status')} "
                                    f"(submitted {at.isoformat() if at else 'unknown'}, {o.get('time_in_force')})",
                                    o.get("client_order_id")))

    # rejections of our own orders
    for o in recent:
        coid = o.get("client_order_id") or ""
        if o.get("status") == "rejected" and coid.startswith(OUR_PREFIXES):
            problems.append(Problem("rejected", o.get("symbol"), f"{coid} was rejected", coid))

    # the Tradebot queue against the account
    for item in queue:
        t = item.get("ticket") or {}
        entry_id, exit_id = t.get("client_order_id"), t.get("exit_order_id")
        if not entry_id:
            continue
        status = item.get("status")
        if status == "sent" and entry_id not in by_id:
            problems.append(Problem("queue_sent_not_on_account", item.get("ticker"),
                                    f"the queue says sent; {entry_id} is not among the account's orders of the window",
                                    entry_id, item.get("id")))
        if status == "ticketed" and entry_id in by_id:
            problems.append(Problem("queue_ticketed_but_on_account", item.get("ticker"),
                                    f"the queue says ticketed; {entry_id} is already on the account "
                                    f"({by_id[entry_id].get('status')})", entry_id, item.get("id")))
        if t.get("exit_done") and exit_id not in by_id:
            problems.append(Problem("queue_exit_done_but_no_exit", item.get("ticker"),
                                    f"the queue says the exit is done; {exit_id} is not on the account", exit_id,
                                    item.get("id")))
    if len(recent) >= PAGE_LIMIT:
        problems.append(Problem("history_truncated", None,
                                f"the account returned {len(recent)} orders, its page limit; older ones may be "
                                "missing, use a shorter --days"))
    return problems


def reconcile(reader: Any, days: int = 10, now: datetime | None = None,
              tickets_dir: Path | None = None) -> Report:
    now = now or datetime.now(UTC)
    after = now - timedelta(days=days)
    positions = reader.positions()
    open_orders = reader.open_orders()
    recent = reader.recent_orders(after)
    expected = expected_orders(None, tickets_dir)
    queue = store.read()["trade"]
    report = Report(now.isoformat(timespec="seconds"), after.isoformat(timespec="seconds"), days)
    ids = {o.get("client_order_id") for o in [*recent, *open_orders]}
    report.counts = {"positions": len(positions), "open_orders": len(open_orders), "recent_orders": len(recent),
                     "tickets_on_file": len(expected),
                     "tickets_on_account": sum(1 for e in expected if e.entry_id in ids), "queue_items": len(queue)}
    report.problems = check(positions, open_orders, recent, expected, queue, now)
    return report


def save(report: Report) -> Path:
    store.assert_not_synced()
    folder = store.data_home() / "status"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(report.created_at).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"reconcile_{stamp}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def latest(folder: Path | None = None) -> tuple[Path, dict] | None:
    folder = folder or store.data_home() / "status"
    found = sorted(folder.glob("reconcile_*.json"))
    return (found[-1], _read(found[-1])) if found else None


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None,
         make_reader: Callable[[Mapping[str, str]], Any] = paper_reader, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the paper account against the tickets and the Tradebot "
                                                 "queue. Read-only.")
    parser.add_argument("--days", type=int, default=10, help="how many days of orders to check (default: 10)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 60:
        print("error: --days must be from 1 to 60", file=sys.stderr)
        return EXIT_BAD_INPUT
    if env is None:
        load_dotenv(REPO_ROOT / ".env")
        env = os.environ
    try:
        report = reconcile(make_reader(env), args.days, now)
    except (NotPaperKey, ReadFailed, store.StoreError) as exc:
        print(f"error: cannot reconcile: {exc}", file=sys.stderr)
        return EXIT_READ_FAILED
    try:
        path = save(report)
    except (OSError, store.StoreError) as exc:
        print(f"warning: the report was not saved: {exc}", file=sys.stderr)
        path = None
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        c = report.counts
        print(f"reconcile {report.created_at}: {c['positions']} positions, {c['open_orders']} open orders, "
              f"{c['recent_orders']} orders since {report.after}, {c['tickets_on_account']} of "
              f"{c['tickets_on_file']} tickets on the account, {c['queue_items']} queue items")
        if report.clean:
            print("  clean: the account matches the tickets and the queue")
        for p in report.problems:
            print(f"  {p.type:<30}{p.symbol or '-':<7}{p.detail}")
        if path:
            print(f"  report: {path}")
    return EXIT_OK if report.clean else EXIT_PROBLEMS


if __name__ == "__main__":
    sys.exit(main())
