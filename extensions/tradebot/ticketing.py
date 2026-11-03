"""Tickets for triggered Tradebot items: levels, size and the book stop-risk across runs. It places no order.

Levels, from the last completed session (``levels_as_of``: the check date after the close, the session before
it during the day, so no forming daily bar is used):

- SIDE (long/short channel items): ``paper_tickets.plan_ticket``: limit at the channel line, stop the channel
  report's clustered swing -/+ 1 ATR snapped, target the ranked handoff's midline; from the newest channel report
  and ranked handoff dated on or before levels_as_of.
- UP and DOWN: ``plan_trend_ticket`` on the daily bars: limit at min(close, SMA20) for a long, max(close, SMA20)
  for a short; stop the last confirmed 5% ZigZag swing low - 1 x ATR(14) snapped down to .99/.49 (long), the last
  swing high + 1 x ATR(14) snapped up to .01/.51 (short); target 2R, entry +/- 2 x the stop distance. The swing,
  SMA20 and ATR are recomputed from the bars (extensions.scanbot.swings, extensions.regime.indicators); the
  scan's stored swing is recorded beside it, with a warning when they differ. Shorts must be shortable and easy
  to borrow (GET /v2/assets/<SYMBOL>, paper keys).

Size, as the SIDE tickets: the basket allocation (``basket_allocation`` of the series' original buying power,
times ``calm_scale`` for SPY and QQQ ADX) unless under the floor, split equally over the names this run tickets, and
each name's share held to NAME_CAP_PCT (15%) of equity, then floor(notional / entry) shares. The 60% gross
exposure cap in paper_orders.guard is the portfolio-level backstop. Then the book stop-risk: open risk is

- each open position's |qty| x |current price - its open stop order's stop price|; a position with no open stop
  order blocks every new ticket (it fails closed),
- each open entry order's ticket risk (tradebot, manual or daily ticket, by client order ID); an open entry no
  ticket file accounts for blocks every new ticket,
- each ticketed item not sent yet,

and the run's tickets are scaled down so open risk + new risk <= BOOK_RISK_PCT of equity. The whole run
(read queue, read account, size, write the ticket file, mark items ticketed) holds the queue lock
(``store.transaction``), so two runs, at once or one after the other, can never both spend the same risk.

Nothing here imports Alpaca: the account, positions, open orders and assets are read through
``paper_orders.snapshot`` (stdlib HTTPS GET only) by the caller and passed in.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from extensions.regime.indicators import sma
from extensions.scanbot.swings import SWING_HIGH, SWING_LOW, wilder_atr, zigzag_swings
from extensions.status import confirmation
from extensions.status.confirmation import MarketData
from extensions.status.daily_status import LONG, SHORT, SIDE
from extensions.status.paper_tickets import (
    BOOK_RISK_PCT,
    STOP_ATR,
    Account,
    Ticket,
    allocation_floor,
    basket_allocation,
    book_risk_limit,
    channel_levels,
    load_series,
    name_notional,
    plan_ticket,
    scale_to_book_risk,
    series_baseline,
    size_ticket,
    snap_stop,
)
from extensions.tradebot import sources, store

TRADEBOT_TICKET_VERSION = 1
TARGET_R = 2.0
NAME_CAP_PCT = 0.15  # of equity (not buying power): no one ticket's notional is larger
SMA_PERIOD = 20
SWING_WARN_PCT = 0.5  # a recomputed swing this far from the scan's stored one is flagged
SWING_FIELD = {LONG: "last_swing_low", SHORT: "last_swing_high"}
SWING_KIND = {LONG: SWING_LOW, SHORT: SWING_HIGH}
ORDER_SIDE = {LONG: "buy", SHORT: "sell"}
STOP_TYPES = {"stop", "stop_limit", "trailing_stop"}


class TicketingError(RuntimeError):
    """The run cannot ticket at all: no account, no index ADX, an unprotected position, an unaccounted entry."""


def tickets_dir() -> Path:
    return store.data_home() / "tickets" / "tradebot"


def name_cap(equity: float) -> float:
    """NAME_CAP_PCT of equity, rounded down to the cent: the most one ticket's notional may be."""
    return math.floor(round(equity * NAME_CAP_PCT * 100, 6)) / 100


def id_prefix(item_id: str, attempt: int) -> str:
    """The client order ID prefix for one ticketing attempt of one item. A new attempt (the entry expired
    unfilled and the item triggered again) gets new IDs, so the sender never mistakes it for the old order."""
    return f"tb-{item_id}-{attempt}"


def entry_order_id(item_id: str, attempt: int, symbol: str, side: str) -> str:
    """As paper_orders.sender.client_order_id builds it from the prefix: <prefix>-<SYMBOL>-<buy|sell>."""
    return f"{id_prefix(item_id, attempt)}-{symbol}-{ORDER_SIDE[side]}"


def exit_order_id(item_id: str, attempt: int, symbol: str) -> str:
    """As paper_orders.sender.exit_order_id: <prefix>-<SYMBOL>-exit."""
    return f"{id_prefix(item_id, attempt)}-{symbol}-exit"


# --- levels ----------------------------------------------------------------------

def _finite(v) -> bool:
    return v is not None and isinstance(v, int | float) and not isinstance(v, bool) and np.isfinite(v) and v > 0


def trend_levels(daily: pd.DataFrame, symbol: str, side: str, as_of: date) -> dict:
    """close, SMA20, ATR(14) and the last confirmed swing low (long) or high (short) on one name's daily bars dated
    on or before as_of. Raises ValueError when they cannot be measured."""
    bars = daily[(daily["symbol"] == symbol) & (daily["date"] <= pd.Timestamp(as_of))] if not daily.empty else daily
    bars = bars.dropna(subset=["high", "low", "close"]).sort_values("date").reset_index(drop=True)
    if bars.empty or bars["date"].iloc[-1].date() != as_of:
        last = bars["date"].iloc[-1].date() if not bars.empty else None
        raise ValueError(f"no daily bar on {as_of} (last {last})")
    if len(bars) < SMA_PERIOD + 1:
        raise ValueError(f"only {len(bars)} daily bars; SMA{SMA_PERIOD} and ATR need {SMA_PERIOD + 1}")
    high, low, close = bars["high"], bars["low"], bars["close"]
    swings = [s for s in zigzag_swings(high, low, close) if s.kind == SWING_KIND[side]]
    swing = max(swings, key=lambda s: s.index) if swings else None
    return {"close": float(close.iloc[-1]), "sma20": float(sma(close, SMA_PERIOD).iloc[-1]),
            "atr": float(wilder_atr(high, low, close).iloc[-1]),
            "swing": None if swing is None else {"price": float(swing.price),
                                                 "date": bars["date"].iloc[swing.index].date().isoformat()}}


def plan_trend_ticket(side: str, close: float, sma20: float, atr: float, swing: dict | None) -> dict:
    """Entry, stop and target for an UP (long) or DOWN (short) name, in plan_ticket's shape. Raises ValueError
    when they cannot make a ticket."""
    if not all(_finite(v) for v in (close, sma20, atr)):
        raise ValueError(f"no usable close, SMA20 or ATR (got {close!r}, {sma20!r}, {atr!r})")
    if not isinstance(swing, dict) or not _finite(swing.get("price")):
        raise ValueError(f"no confirmed swing {'low' if side == LONG else 'high'} on the daily bars")
    sign = 1 if side == LONG else -1
    entry = round(min(close, sma20) if side == LONG else max(close, sma20), 2)
    calculated = round(swing["price"] - sign * STOP_ATR * atr, 4)
    stop = snap_stop(side, calculated)
    if not stop > 0:
        raise ValueError(f"stop {stop} from swing {swing['price']} -/+ {STOP_ATR:g} x ATR {atr} is not above zero")
    stop_offset = round(sign * (entry - stop), 2)
    if not stop_offset > 0:
        raise ValueError(f"stop {stop} is not beyond entry {entry} (the swing is on the wrong side of the entry)")
    target = round(entry + sign * TARGET_R * stop_offset, 2)
    if not target > 0:
        raise ValueError(f"the {TARGET_R:g}R target {target} is not above zero")
    reward = round(sign * (target - entry), 2)
    return {"entry": entry, "stop": stop, "stop_offset": stop_offset, "target": target, "line": "sma20_pullback",
            "channel_level": round(sma20, 4), "atr_14": round(atr, 4), "midline": None, "stop_swing": swing["price"],
            "stop_swing_date": swing.get("date"), "stop_calculated": calculated, "risk_per_share": stop_offset,
            "reward_per_share": reward}


def plan_side_item(item: dict, levels_as_of: date) -> dict:
    """plan_ticket for a long/short channel item, from the newest channel report and handoff on or before."""
    kind, side, symbol = item["kind"], item["side"], item["ticker"]
    found = sources.newest_scan(kind, None, levels_as_of, on_or_before=True)
    if found is None:
        raise ValueError(f"no {side} channel report on or before {levels_as_of}")
    report_path, report = found
    handoff = sources.newest(sources.folder("handoffs"), "ranked_side_handoff_{date}_*.json", levels_as_of,
                             on_or_before=True)
    midline = (((handoff[1].get("names") or {}).get(symbol) or {}).get("levels") or {}).get("midline") \
        if handoff else None
    line, atr, swing = channel_levels(report, symbol, side)
    plan = plan_ticket(symbol, side, line, atr, midline, swing)
    plan["levels_from"] = {"channel_report": report_path.name, "ranked_handoff": handoff[0].name if handoff else None}
    return plan


# --- open risk ---------------------------------------------------------------------

def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def ticket_risk_index(queue_items: list[dict] | None = None) -> dict[str, float]:
    """client order ID -> the stop-risk dollars of the ticket it was sent from, for every ticket file on disk
    (tradebot, manual, daily) and every ticketed queue item."""
    out: dict[str, float] = {}
    base = store.data_home() / "tickets"

    def add(coid: str, t: dict) -> None:
        risk = _num(t.get("risk_dollars"))
        if risk is None:
            q, off = _num(t.get("quantity")), _num(t.get("stop_offset"))
            risk = q * off if q is not None and off is not None else None
        if risk is not None:
            out[coid] = round(risk, 2)

    for path in sorted(base.glob("paper_tickets_*.json")):
        data = _read_json(path)
        for t in data.get("tickets") or []:
            add(f"pt-{data.get('as_of')}-{t.get('symbol')}-{t.get('order_side')}", t)
    for path in sorted((base / "manual").glob("manual_ticket_*.json")):
        data = _read_json(path)
        for t in data.get("tickets") or []:
            add(f"mt-{data.get('as_of')}-{data.get('id')}-{t.get('symbol')}-{t.get('order_side')}", t)
    for path in sorted((base / "tradebot").glob("tradebot_tickets_*.json")):
        for t in _read_json(path).get("tickets") or []:
            if t.get("client_order_id"):
                add(t["client_order_id"], t)
    for item in queue_items or []:
        t = (item.get("ticket") or {})
        if t.get("client_order_id"):
            add(t["client_order_id"], t)
    return out


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TicketingError(f"cannot read ticket file {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _flat_orders(orders: list[dict]) -> list[dict]:
    out = []
    for o in orders:
        out.append(o)
        out.extend(o.get("legs") or [])
    return out


@dataclass
class RiskBook:
    equity: float
    limit: float
    positions: list[dict] = field(default_factory=list)
    open_entries: list[dict] = field(default_factory=list)
    ticketed: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def open_risk(self) -> float:
        return round(sum(r["risk"] for r in (*self.positions, *self.open_entries, *self.ticketed)), 2)

    @property
    def room(self) -> float:
        return round(max(0.0, self.limit - self.open_risk), 2)

    def to_dict(self) -> dict:
        return {**asdict(self), "open_risk": self.open_risk, "room": self.room,
                "book_risk_pct_of_equity": BOOK_RISK_PCT}


def risk_book(account: Account, positions: list[dict], open_orders: list[dict], queue_items: list[dict]) -> RiskBook:
    """The stop-risk already on the book. problems is non-empty when some of it cannot be measured."""
    book = RiskBook(account.equity, book_risk_limit(account.equity))
    flat = _flat_orders(open_orders)
    for p in positions:
        symbol, qty, price = p.get("symbol"), _num(p.get("qty")), _num(p.get("current_price"))
        if qty is None or price is None:
            book.problems.append(f"position {symbol}: no usable qty or current_price")
            continue
        stops = [_num(o.get("stop_price")) for o in flat
                 if o.get("symbol") == symbol and (o.get("type") or o.get("order_type")) in STOP_TYPES
                 and _num(o.get("stop_price")) is not None]
        if not stops:
            book.problems.append(f"unprotected position {symbol}: no open stop order")
            continue
        stop = min(stops, key=lambda s: abs(price - s))  # the nearest stop is the one that fires first
        book.positions.append({"symbol": symbol, "qty": qty, "price": price, "stop": stop,
                               "risk": round(abs(qty) * abs(price - stop), 2)})
    index = ticket_risk_index(queue_items)
    for o in open_orders:
        coid = o.get("client_order_id") or ""
        if (o.get("order_class") or "simple") != "simple" or coid.endswith("-exit") or o.get("legs"):
            continue  # exits and brackets carry their own stop; the position covers them
        if (o.get("type") or o.get("order_type")) in STOP_TYPES:
            continue
        risk = index.get(coid)
        if risk is None:
            book.problems.append(f"open entry {coid or o.get('id')} ({o.get('symbol')}) has no ticket to measure "
                                 "its stop-risk")
            continue
        filled = _num(o.get("filled_qty")) or 0.0
        qty = _num(o.get("qty")) or 0.0
        left = (qty - filled) / qty if qty > 0 else 1.0  # the filled part is already a position
        book.open_entries.append({"symbol": o.get("symbol"), "client_order_id": coid,
                                  "risk": round(risk * left, 2)})
    for item in queue_items:
        if item.get("status") == "ticketed" and item.get("ticket"):
            book.ticketed.append({"symbol": item["ticker"], "item_id": item["id"],
                                  "risk": round(_num(item["ticket"].get("risk_dollars")) or 0.0, 2)})
    return book


# --- the run ---------------------------------------------------------------------------

@dataclass
class TicketRun:
    as_of: date
    levels_as_of: date
    created_at: datetime
    tickets: list[dict] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    basket: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    no_ticket_reason: str | None = None
    file: str | None = None
    dry_run: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(as_of=self.as_of.isoformat(), levels_as_of=self.levels_as_of.isoformat(),
                 created_at=self.created_at.isoformat(timespec="seconds"))
        return d


def plan_item(item: dict, market: MarketData, levels_as_of: date,
              read_asset: Callable[[str], dict] | None) -> tuple[dict | None, str | None, list[str]]:
    """(plan, why not, warnings) for one triggered item."""
    warnings: list[str] = []
    side, bot = item["side"], item.get("bot")
    try:
        if bot == SIDE:
            if item.get("kind") not in ("long", "short"):
                return None, (f"no level rule for a SIDE {item.get('kind')} item; use the Trade page's manual "
                              "ticket"), warnings
            plan = plan_side_item(item, levels_as_of)
        elif bot in (sources.UP, sources.DOWN):
            want = LONG if bot == sources.UP else SHORT
            if side != want:
                return None, f"a {bot} item must be {want}, not {side}", warnings
            lv = trend_levels(market.daily, item["ticker"], side, levels_as_of)
            plan = plan_trend_ticket(side, lv["close"], lv["sma20"], lv["atr"], lv["swing"])
            stored = _num((item.get("metrics") or {}).get(SWING_FIELD[side]))
            plan["stored_swing"] = stored
            if stored is not None and lv["swing"] and \
                    abs(lv["swing"]["price"] - stored) / stored * 100 > SWING_WARN_PCT:
                warnings.append(f"{item['ticker']}: swing {lv['swing']['price']:.2f} from today's bars differs from "
                                f"the scan's {stored:.2f}; the fresh one is used")
        else:
            return None, f"unknown bot {bot!r}", warnings
    except ValueError as exc:
        return None, str(exc), warnings
    if side == SHORT:
        if read_asset is None:
            return None, "cannot check the short borrow: no asset reader", warnings
        try:
            asset = read_asset(item["ticker"])
        except Exception as exc:  # noqa: BLE001 - any failure to read the asset fails closed
            return None, f"cannot check the short borrow: {exc}", warnings
        if not (asset.get("shortable") is True and asset.get("easy_to_borrow") is True):
            return None, (f"not shortable and easy to borrow (shortable {asset.get('shortable')}, easy_to_borrow "
                          f"{asset.get('easy_to_borrow')})"), warnings
    return plan, None, warnings


def build_run(items: list[dict], as_of: date, levels_as_of: date, account: Account, positions: list[dict],
              open_orders: list[dict], queue_items: list[dict], market: MarketData,
              read_asset: Callable[[str], dict] | None, original_buying_power: float,
              created_at: datetime | None = None) -> TicketRun:
    """Tickets for the triggered items. Raises TicketingError when the run cannot size at all."""
    run = TicketRun(as_of, levels_as_of, created_at or datetime.now(UTC))
    book = risk_book(account, positions, open_orders, queue_items)
    run.risk = book.to_dict()
    if book.problems:
        raise TicketingError("cannot measure the book's stop-risk: " + "; ".join(book.problems))

    readings = confirmation.index_adx(market.daily, levels_as_of)
    missing = [s for s, v in readings.items() if v is None]
    if missing:
        raise TicketingError(f"no ADX(14) on a {levels_as_of} daily bar for {', '.join(missing)}; the basket scale "
                             "needs SPY and QQQ")
    planned: list[tuple[dict, dict]] = []
    for item in items:
        plan, why, warns = plan_item(item, market, levels_as_of, read_asset)
        run.warnings += warns
        if plan is None:
            run.skipped[item["ticker"]] = why
        else:
            planned.append((item, plan))

    scale = confirmation.calm_scale(readings)
    before = basket_allocation(original_buying_power, account.buying_power)
    allocation = math.floor(before * scale * 100) / 100
    run.basket = {"original_buying_power": original_buying_power, "buying_power": account.buying_power,
                  "equity": account.equity, "index_adx": readings, "calm_scale": scale,
                  "allocation_before_calm_scale": before, "allocation": allocation,
                  "floor": allocation_floor(account.equity), "names_n": len(planned)}
    if not planned:
        run.no_ticket_reason = "no triggered item made a plan"
        return run
    if allocation < run.basket["floor"]:
        run.no_ticket_reason = (f"allocation ${allocation:,.2f} is under the floor ${run.basket['floor']:,.2f}; "
                                "no tickets")
        return run
    if book.room <= 0:
        run.no_ticket_reason = (f"the book's stop-risk ${book.open_risk:,.2f} already fills the "
                                f"{BOOK_RISK_PCT:.0%} limit ${book.limit:,.2f}; no tickets")
        for item, _ in planned:
            run.skipped[item["ticker"]] = run.no_ticket_reason
        return run

    split = name_notional(allocation, len(planned))
    cap = name_cap(account.equity)
    notional = min(split, cap)
    run.basket.update(name_notional_split=split, name_cap=cap, name_cap_pct_of_equity=NAME_CAP_PCT,
                      name_notional=notional)
    if notional < split:
        run.warnings.append(f"per-name notional ${split:,.2f} capped at ${cap:,.2f} "
                            f"({NAME_CAP_PCT:.0%} of equity ${account.equity:,.2f})")
    sized: list[Ticket] = []
    by_symbol: dict[str, dict] = {}
    for item, plan in planned:
        extra = {k: plan.pop(k) for k in ("levels_from", "stored_swing") if k in plan}
        try:
            t = size_ticket(item["ticker"], item["side"], plan, notional_limit=notional)
        except ValueError as exc:
            run.skipped[item["ticker"]] = str(exc)
            continue
        sized.append(t)
        by_symbol[t.symbol] = {"item": item, "extra": extra}
    run.basket["risk_before_scale"] = round(sum(t.risk_dollars for t in sized), 2)
    run.basket["room"] = book.room
    kept, scaled_out, run.basket["scale"] = scale_to_book_risk(sized, book.room)
    run.skipped.update(scaled_out)
    for t in kept:
        item = by_symbol[t.symbol]["item"]
        attempt = int(item.get("attempts") or 0) + 1
        run.tickets.append({**asdict(t), **by_symbol[t.symbol]["extra"], "item_id": item["id"], "bot": item["bot"],
                            "kind": item["kind"], "attempt": attempt, "id_prefix": id_prefix(item["id"], attempt),
                            "client_order_id": entry_order_id(item["id"], attempt, t.symbol, t.side),
                            "exit_order_id": exit_order_id(item["id"], attempt, t.symbol)})
    if not run.tickets:
        run.no_ticket_reason = "no triggered item made a ticket"
    return run


def save_run(run: TicketRun) -> Path:
    folder = tickets_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"tradebot_tickets_{run.as_of}_{run.created_at:%Y%m%dT%H%M%S%fZ}.json"
    data = {"kind": "tradebot", "tradebot_ticket_version": TRADEBOT_TICKET_VERSION, "paper": True,
            "orders_placed": False, **run.to_dict()}
    data.pop("file", None)
    data.pop("dry_run", None)
    with path.open("x", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    return path


def ticket_triggered(as_of: date, levels_as_of: date, market: MarketData, read_account: Callable[[], Account],
                     read_positions: Callable[[], list[dict]], read_open_orders: Callable[[], list[dict]],
                     read_asset: Callable[[str], dict] | None, item_ids: list[str] | None = None,
                     dry_run: bool = False, series_file: Path | None = None) -> TicketRun:
    """Ticket every triggered item (or those in item_ids) under the queue lock; with dry_run, write nothing."""
    series = series_file or series_path()

    def run_on(queues: dict) -> TicketRun:
        items = [i for i in queues["trade"] if i.get("status") == "triggered"
                 and (item_ids is None or i["id"] in item_ids)]
        account = read_account()
        original = _series_original(series, account) if dry_run else series_baseline(series, account, levels_as_of)[1]
        run = build_run(items, as_of, levels_as_of, account, read_positions(), read_open_orders(), queues["trade"],
                        market, read_asset, original)
        run.dry_run = dry_run
        return run

    if dry_run:
        return run_on(store.read())
    with store.transaction() as queues:
        run = run_on(queues)
        if run.tickets:
            path = save_run(run)
            run.file = path.name
            for t in run.tickets:
                item = store.find(queues, t["item_id"])
                item["attempts"] = t["attempt"]
                item["ticket"] = {"file": path.name, "id_prefix": t["id_prefix"], "attempt": t["attempt"],
                                  "client_order_id": t["client_order_id"],
                                  "exit_order_id": t["exit_order_id"], "risk_dollars": t["risk_dollars"],
                                  "entry": t["entry"], "stop": t["stop"], "target": t["target"],
                                  "quantity": t["quantity"]}
                store.set_status(item, "ticketed", f"ticketed in {path.name}")
        for symbol, why in run.skipped.items():
            item = next((i for i in queues["trade"] if i["ticker"] == symbol and i.get("status") == "triggered"),
                        None)
            if item is not None:
                store.add_check(item, {"stage": "ticket", "status": None, "passed": False, "reason": why})
    return run


def series_path() -> Path:
    """The series file the SIDE tickets also use (paper_tickets.default_series_file), under the data home."""
    return store.data_home() / "tickets" / "series.json"


def _series_original(path: Path, account: Account) -> float:
    """The series' original buying power without recording anything (a dry run never writes the series file)."""
    data = load_series(path)
    return float(data["original_buying_power"]) if data else account.buying_power
