"""Position sizes for the backtest, in whole shares, and the book of open positions the limits are checked on.

Two modes:

- **live** (default): what runs live today, the Tradebot ticketing (``extensions/tradebot/ticketing.build_run``) and
  the sender's risk guard (``paper_orders/guard.py``), called through their own functions:
  1. allocation = ``basket_allocation`` x ``calm_scale`` (SPY and QQQ ADX(14) both under 15 on the run date: x 0.7);
     under ``allocation_floor`` (5%) the basket gets nothing;
  2. each of the basket's N names gets min(``name_notional(allocation, N)``, ``name_cap``: 15%) and
     ``size_ticket`` makes floor(notional / entry) whole shares at the planned entry;
  3. ``scale_to_book_risk`` holds the basket to the book's room: 8% of capital less the stop-risk of the positions
     still open;
  4. the guard, ticket by ticket in rank order, skipping only the one that would break a check: a symbol already
     held, max positions (15), gross exposure (60% of capital).
- **fixed**: floor(capital x risk % / risk per share at the fill) shares, held to a position value of at most the
  capital (marked ``capped``); the limits are off unless set.

Starting capital is the equity on every day (the 15% name cap, the 8% book stop-risk and the 60% gross limit are
shares of it), with no compounding. Buying power (default: the starting capital) stands in for the series' original
buying power and the buying power read at ticket time, so the 23% basket is a share of it, as live, where Alpaca's
buying power includes margin; the guard's daily-loss and drawdown stops are not simulated. The book's stop-risk is each open
position's shares x its risk per share at the fill (live reads the current price against the stop order).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from extensions.status import confirmation
from extensions.status.paper_tickets import (BOOK_RISK_PCT, LONG, allocation_floor, basket_allocation,
                                             name_notional, scale_to_book_risk, size_ticket)
from extensions.tradebot.ticketing import NAME_CAP_PCT, name_cap
from paper_orders.guard import DEFAULT_RISK

LIVE, FIXED = "live", "fixed"
SIZINGS = (LIVE, FIXED)
LIVE_LIMITS = {"max_positions": int(DEFAULT_RISK["max_positions"]),
               "max_gross_pct": float(DEFAULT_RISK["max_gross_exposure_pct"]),
               "max_open_risk_pct": round(BOOK_RISK_PCT * 100, 6)}
FIXED_LIMITS = {"max_positions": 0, "max_gross_pct": 0.0, "max_open_risk_pct": 0.0}


@dataclass(frozen=True)
class Limits:
    """0 means no limit."""
    max_positions: int = 0
    max_gross_pct: float = 0.0
    max_open_risk_pct: float = 0.0


@dataclass
class Book:
    """One origin's positions taken so far (trade dicts with qty, notional, risk_dollars, entry/exit dates)."""
    capital: float
    positions: list[dict] = field(default_factory=list)

    def open_on(self, day: str) -> list[dict]:
        """Positions held on ``day``: entered on or before it and not exited before it."""
        return [t for t in self.positions if t["entry_date"] <= day <= t["exit_date"]]

    def open_risk(self, day: str) -> float:
        return sum(t["risk_dollars"] for t in self.open_on(day))

    def gross(self, day: str) -> float:
        return sum(t["notional"] for t in self.open_on(day))

    def holds(self, symbol: str, day: str) -> str | None:
        """The exit date of an open position in ``symbol`` on ``day`` (any bot), else None."""
        for t in self.open_on(day):
            if t["symbol"] == symbol:
                return t["exit_date"]
        return None


def guard_reason(book: Book, limits: Limits, symbol: str, day: str, notional: float, risk: float) -> str | None:
    """Why the guard would skip this position on ``day``, or None. Checked in the guard's order."""
    held = book.holds(symbol, day)
    if held is not None:
        return f"already holding {symbol} until {held}"
    open_now = book.open_on(day)
    if limits.max_positions and len(open_now) + 1 > limits.max_positions:
        return f"{len(open_now)} positions already open; the limit is {limits.max_positions}"
    if limits.max_gross_pct:
        gross = sum(t["notional"] for t in open_now) + notional
        if gross > book.capital * limits.max_gross_pct / 100 + 1e-6:
            return (f"gross exposure would be {gross / book.capital * 100:.1f}% of capital "
                    f"(limit {limits.max_gross_pct:g}%)")
    if limits.max_open_risk_pct:
        total = sum(t["risk_dollars"] for t in open_now) + risk
        if total > book.capital * limits.max_open_risk_pct / 100 + 1e-6:
            return (f"open stop-risk would be {total / book.capital * 100:.2f}% of capital "
                    f"(limit {limits.max_open_risk_pct:g}%)")
    return None


def calm_scale_for(daily: pd.DataFrame, as_of) -> tuple[float, dict, bool]:
    """(scale, readings, fallback): the live calm scale from SPY and QQQ daily bars up to ``as_of``. With a reading
    missing (live refuses to ticket then) the scale is 1.0 and ``fallback`` is True."""
    readings = confirmation.index_adx(daily, as_of)
    if any(v is None for v in readings.values()):
        return 1.0, readings, True
    return confirmation.calm_scale(readings), readings, False


def _ticket_plan(entry: float, risk_per_share: float, side: str) -> dict:
    """The plan fields ``size_ticket`` needs; only entry and risk per share set the size."""
    sign = 1 if side == LONG else -1
    return {"entry": entry, "stop": round(entry - sign * risk_per_share, 4), "stop_offset": risk_per_share,
            "target": entry, "line": "support" if side == LONG else "resistance", "channel_level": entry,
            "atr_14": 0.0, "midline": entry, "stop_swing": entry, "stop_swing_date": None,
            "stop_calculated": entry, "risk_per_share": risk_per_share, "reward_per_share": risk_per_share}


def live_basket(names: list[dict], capital: float, scale: float, room: float,
                buying_power: float | None = None) -> tuple[dict[str, int], dict[str, str], dict]:
    """(shares by key, skip reasons by key, basket record) for one basket. ``names`` are dicts with key, symbol,
    side, entry, risk_per_share, in rank order. ``capital`` is the equity; ``buying_power`` (default the capital)
    the original and current buying power the basket is a share of."""
    bp = capital if buying_power is None else buying_power
    before = basket_allocation(bp, bp)
    allocation = math.floor(before * scale * 100) / 100
    info = {"allocation_before_calm_scale": before, "calm_scale": scale, "allocation": allocation,
            "floor": allocation_floor(capital), "names_n": len(names), "room": round(room, 2)}
    if not names:
        return {}, {}, info
    if allocation < info["floor"]:
        why = f"allocation ${allocation:,.2f} is under the floor ${info['floor']:,.2f}; no tickets"
        return {}, {n["key"]: why for n in names}, info
    if room <= 0:
        why = (f"the book's stop-risk already fills the {BOOK_RISK_PCT:.0%} limit "
               f"${capital * BOOK_RISK_PCT:,.2f}; no tickets")
        return {}, {n["key"]: why for n in names}, info
    split = name_notional(allocation, len(names))
    notional = min(split, name_cap(capital))
    info.update(name_notional_split=split, name_cap=name_cap(capital), name_cap_pct=NAME_CAP_PCT,
                name_notional=notional)
    tickets, skipped = [], {}
    for n in names:
        try:
            t = size_ticket(n["key"], n["side"], _ticket_plan(n["entry"], n["risk_per_share"], n["side"]),
                            notional_limit=notional)
        except ValueError as exc:
            skipped[n["key"]] = str(exc)
            continue
        tickets.append(t)
    kept, scaled_out, info["book_scale"] = scale_to_book_risk(tickets, room)
    skipped.update(scaled_out)
    return {t.symbol: t.quantity for t in kept}, skipped, info


def fixed_quantity(capital: float, risk_pct: float, risk_per_share: float, entry: float,
                   buying_power: float | None = None) -> tuple[int, bool]:
    """(shares, capped): floor(capital x risk % / risk per share), held to a position value of at most the buying
    power (default the capital)."""
    bp = capital if buying_power is None else buying_power
    qty = math.floor(round(capital * risk_pct / 100 / risk_per_share, 6)) if risk_per_share > 0 else 0
    most = math.floor(round(bp / entry, 6)) if entry > 0 else 0
    return (most, True) if qty > most else (qty, False)


def dollars(trade: dict, qty: int, commission_per_share: float) -> dict:
    """The whole-share figures for a simulated trade: qty, notional, risk_dollars, fees, pnl_usd."""
    sign = 1 if trade["side"] == LONG else -1
    fees = round(2 * qty * commission_per_share, 2)
    return {"qty": qty, "notional": round(qty * trade["entry"], 2),
            "risk_dollars": round(qty * trade["risk_per_share"], 2), "fees": fees,
            "pnl_usd": round(qty * sign * (trade["exit"] - trade["entry"]) - fees, 2)}
