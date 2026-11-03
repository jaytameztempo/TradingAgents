"""The risk guard: which entries may go to the paper account right now. Exits are never its business.

``paper_orders.sender`` calls ``filter_entries`` before every entry it sends, in every mode (--tradebot,
--as-of, --manual), with the account, open positions and open orders it just read. In order:

1. kill switch: on, no entry goes,
2. daily loss: equity down daily_loss_pct (3%) or more from the last close's equity (Alpaca's last_equity),
   no entry goes,
3. drawdown: equity down drawdown_pct (10%) or more from the highest equity seen (``risk_state``), no entry goes,
4. already held: a symbol with an open position or any open order is skipped,
5. positions: open positions plus open entries plus this batch held to max_positions (15),
6. gross exposure: |market value| of the positions plus the notional of open entries plus this batch held to
   max_gross_exposure_pct (60%) of equity,
7. book stop-risk: the stop-risk already on the book (``extensions.tradebot.ticketing.risk_book``, minus the
   tickets in this batch) plus this batch held to 8% of equity; a position with no stop, or an open entry no
   ticket file accounts for, stops every entry.

Tickets are taken in the order given and each check skips only the tickets that would break it. Settings come
from the "risk" section of settings.json (the Settings page); a missing value takes its default, a value that is
present but not valid stops every entry. Missing or unusable account numbers stop every entry too.

This module imports no Alpaca package: it works on the plain dicts the sender's client returns.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from extensions.status.paper_tickets import BOOK_RISK_PCT, Account
from extensions.tradebot.ticketing import TicketingError, risk_book
from paper_orders import risk_state
from paper_orders.trade_window import SettingsError, load_settings, settings_path

DEFAULT_RISK: dict[str, Any] = {
    "kill_switch": False,
    "daily_loss_pct": 3.0,
    "drawdown_pct": 10.0,
    "max_positions": 15,
    "max_gross_exposure_pct": 60.0,
}
LIMITS = {"daily_loss_pct": (0.1, 50.0), "drawdown_pct": (0.1, 90.0), "max_gross_exposure_pct": (1.0, 400.0)}
MAX_POSITIONS = (1, 200)
NOTE_KEYS = ("kill_switch_at", "kill_switch_reason")  # recorded with the switch, not checked


def validate_risk(data: Any) -> dict:
    """The risk section with defaults filled in. Raises SettingsError for any value that is present and wrong."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SettingsError("the risk settings are not an object")
    unknown = set(data) - set(DEFAULT_RISK) - set(NOTE_KEYS)
    if unknown:
        raise SettingsError(f"unknown risk setting(s): {', '.join(sorted(unknown))}")
    out = {**DEFAULT_RISK, **data}
    if not isinstance(out["kill_switch"], bool):
        raise SettingsError("risk kill_switch must be true or false")
    for key, (low, high) in LIMITS.items():
        v = out[key]
        if isinstance(v, bool) or not isinstance(v, int | float) or not low <= v <= high:
            raise SettingsError(f"risk {key} must be a number from {low:g} to {high:g}")
        out[key] = float(v)
    v = out["max_positions"]
    if isinstance(v, bool) or not isinstance(v, int) or not MAX_POSITIONS[0] <= v <= MAX_POSITIONS[1]:
        raise SettingsError(f"risk max_positions must be a whole number from {MAX_POSITIONS[0]} to {MAX_POSITIONS[1]}")
    return out


def load_risk() -> dict:
    return validate_risk(load_settings().get("risk"))


def set_kill_switch(on: bool, reason: str) -> dict:
    """Turn the kill switch on or off in settings.json, keeping every other setting. Returns the risk section."""
    path = settings_path()
    data = load_settings(path)
    section = validate_risk(data.get("risk"))
    section.update(kill_switch=on, kill_switch_at=datetime.now(UTC).isoformat(timespec="seconds"),
                   kill_switch_reason=reason)
    data["risk"] = section
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return section


# --- the decision ----------------------------------------------------------------

@dataclass
class Decision:
    allowed: list[dict] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # symbol -> why
    blocked: str | None = None  # why no entry may go at all
    numbers: dict = field(default_factory=dict)
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def reason_for(self, symbol: str) -> str | None:
        return self.blocked or self.skipped.get(symbol)

    def to_dict(self) -> dict:
        return asdict(self)


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _is_entry(order: dict) -> bool:
    """An open order that opens a position: a plain order that is not an exit and not a stop."""
    if (order.get("order_class") or "simple") != "simple" or order.get("legs"):
        return False
    if (order.get("client_order_id") or "").endswith("-exit"):
        return False
    return (order.get("type") or order.get("order_type")) not in ("stop", "stop_limit", "trailing_stop")


def filter_entries(tickets: list[dict], account: dict, positions: list[dict], open_orders: list[dict],
                   risk: dict, state: dict, queue_items: list[dict] | None = None,
                   batch_ids: set[str] | None = None, now: datetime | None = None) -> tuple[Decision, dict]:
    """(decision, new risk state). batch_ids are the client order IDs of the tickets in this batch, so their own
    ticketed queue items are not counted twice in the book stop-risk."""
    d = Decision()
    equity, last_equity = _num(account.get("equity")), _num(account.get("last_equity"))
    buying_power = _num(account.get("buying_power"))
    d.numbers = {"equity": equity, "last_equity": last_equity, "buying_power": buying_power, "risk": risk}

    def block(why: str) -> tuple[Decision, dict]:
        d.blocked = why
        d.skipped.update({t["symbol"]: why for t in tickets})
        return d, state

    if risk["kill_switch"]:
        return block(f"kill switch is on ({risk.get('kill_switch_reason') or 'no reason given'})")
    if equity is None or equity <= 0 or last_equity is None or last_equity <= 0:
        return block(f"the account shows no usable equity / last_equity ({account.get('equity')!r}, "
                     f"{account.get('last_equity')!r})")

    day_loss = (last_equity - equity) / last_equity * 100
    d.numbers["daily_loss_pct"] = round(day_loss, 2)
    if day_loss >= risk["daily_loss_pct"]:
        return block(f"daily loss {day_loss:.2f}% >= {risk['daily_loss_pct']:g}% (equity {equity:,.2f}, "
                     f"last close {last_equity:,.2f})")

    state = risk_state.record_equity(state, equity, now)
    peak = state["peak_equity"]
    drawdown = (peak - equity) / peak * 100
    d.numbers.update(peak_equity=peak, drawdown_pct=round(drawdown, 2))
    if drawdown >= risk["drawdown_pct"]:
        return block(f"drawdown {drawdown:.2f}% >= {risk['drawdown_pct']:g}% from the peak {peak:,.2f}")

    held = {p.get("symbol") for p in positions} | {o.get("symbol") for o in open_orders}
    entries = [o for o in open_orders if _is_entry(o)]
    count = len({p.get("symbol") for p in positions} | {o.get("symbol") for o in entries})
    gross = sum(abs(_num(p.get("market_value")) or 0.0) for p in positions)
    gross += sum((_num(o.get("qty")) or 0.0) * (_num(o.get("limit_price")) or 0.0) for o in entries)
    gross_limit = equity * risk["max_gross_exposure_pct"] / 100

    acct = Account(equity, buying_power or 0.0, now or datetime.now(UTC), "guard")
    batch_ids = batch_ids or set()
    others = [i for i in (queue_items or []) if (i.get("ticket") or {}).get("client_order_id") not in batch_ids]
    try:
        book = risk_book(acct, positions, open_orders, others)
    except TicketingError as exc:
        return block(str(exc))
    if book.problems:
        return block("cannot measure the book's stop-risk: " + "; ".join(book.problems))
    room = book.room
    d.numbers.update(open_positions=len(positions), open_entries=len(entries), count_before=count,
                     gross_before=round(gross, 2), gross_limit=round(gross_limit, 2),
                     book_open_risk=book.open_risk, book_limit=book.limit, book_room=room,
                     book_risk_pct_of_equity=BOOK_RISK_PCT)

    for t in tickets:
        symbol = t["symbol"]
        notional = t["quantity"] * t["entry"]
        risk_dollars = _num(t.get("risk_dollars"))
        if risk_dollars is None:
            risk_dollars = t["quantity"] * t["stop_offset"]
        if symbol in held:
            d.skipped[symbol] = f"{symbol} already has an open position or open order"
        elif count + 1 > risk["max_positions"]:
            d.skipped[symbol] = f"{count} positions and open entries; the limit is {risk['max_positions']}"
        elif gross + notional > gross_limit:
            d.skipped[symbol] = (f"gross exposure {gross:,.2f} + {notional:,.2f} would pass "
                                 f"{risk['max_gross_exposure_pct']:g}% of equity ({gross_limit:,.2f})")
        elif risk_dollars > room + 0.005:
            d.skipped[symbol] = (f"stop-risk {risk_dollars:,.2f} is over the book's room {room:,.2f} "
                                 f"({BOOK_RISK_PCT:.0%} of equity, {book.open_risk:,.2f} already at risk)")
        else:
            d.allowed.append(t)
            held.add(symbol)
            count += 1
            gross += notional
            room = round(room - risk_dollars, 2)
    d.numbers.update(count_after=count, gross_after=round(gross, 2), book_room_after=room)
    return d, state


def describe(d: Decision) -> list[str]:
    """Lines for the sender's output."""
    n = d.numbers
    lines = []
    if n.get("equity") is not None:
        line = f"guard: equity {n['equity']:,.2f}"
        if n.get("daily_loss_pct") is not None:
            line += f", day {-n['daily_loss_pct']:+.2f}%"
        if n.get("drawdown_pct") is not None:
            line += f", drawdown {n['drawdown_pct']:.2f}% from {n['peak_equity']:,.2f}"
        if n.get("gross_limit") is not None:
            line += (f"; positions {n['count_before']}/{n['risk']['max_positions']}, gross "
                     f"{n['gross_before']:,.2f}/{n['gross_limit']:,.2f}, stop-risk room {n['book_room']:,.2f}")
        lines.append(line)
    if d.blocked:
        lines.append(f"guard: NO ENTRIES: {d.blocked}")
    else:
        for t in d.allowed:
            lines.append(f"  {t['symbol']:<6}allowed")
        for symbol, why in d.skipped.items():
            lines.append(f"  {symbol:<6}SKIPPED   {why}")
    return lines


__all__ = ["DEFAULT_RISK", "Decision", "describe", "filter_entries", "load_risk", "set_kill_switch",
           "validate_risk"]
