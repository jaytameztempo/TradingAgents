"""Paper-order tickets for the SIDE names still allowed after the earnings blackout. It places no orders.

It reads three saved files for one date and nothing else:

- the newest daily status (the market label and the ranked side handoff it used),
- the newest earnings blackout, which must be built from that daily status
  (its ``allowed`` long and short names are the only names ticketed),
- the ranked side handoff named in the daily status (each name's recorded
  ``support``, ``resistance``, daily ``atr_14`` and ``midline``).

Under any label but SIDE, or when the daily status blocks the channel names,
there are no tickets. Each ticket:

- entry: the channel line, support for a long and resistance for a short,
- stop: ``STOP_ATR`` (0.25) ATR beyond that line, below support or above resistance,
- target: the channel midline,
- quantity: the smaller of whole shares risking ``RISK_PCT`` (1%) of the
  placeholder account between entry and stop, and whole shares costing at most
  ``NOTIONAL_PCT`` (10%) of it at entry. Both limits and the one that bound the
  size are recorded.

Every file is read with plain ``json``; no Alpaca module is loaded here, and
``refuse_trading_client`` makes any later import of ``alpaca.trading`` fail.
The scans, router, bots, daily status and blackout are not touched.
"""

from __future__ import annotations

import importlib.abc
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from extensions.status.daily_status import LONG, SHORT, SIDE, SIDES, default_handoff_dir, default_status_dir
from extensions.status.earnings_blackout import BlackoutInputError, latest_daily_status

TICKET_VERSION = 1
BLACKOUT_REPORT_VERSION = 1
ACCOUNT_EQUITY = 100_000.0  # placeholder, not a broker balance
RISK_PCT = 0.01
NOTIONAL_PCT = 0.10
STOP_ATR = 0.25
LINE = {LONG: "support", SHORT: "resistance"}
RISK, NOTIONAL = "risk", "notional"
TRADING_MODULE = "alpaca.trading"


class TicketInputError(RuntimeError):
    """A saved file is missing, cannot be read, or does not chain to the others."""


class TradingClientLoaded(RuntimeError):
    """Alpaca's trading client is already loaded in this interpreter."""


# --- the trading client stays out --------------------------------------------

def _is_trading_module(name: str) -> bool:
    return name == TRADING_MODULE or name.startswith(TRADING_MODULE + ".")


class _RefuseTradingClient(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if _is_trading_module(fullname):
            raise ImportError(f"{fullname} is refused: paper tickets never load Alpaca's trading client")
        return None


def refuse_trading_client() -> None:
    """Stop if alpaca.trading is loaded, then make every later import of it raise ImportError."""
    loaded = sorted(m for m in sys.modules if _is_trading_module(m))
    if loaded:
        raise TradingClientLoaded(f"Alpaca's trading client is already loaded: {', '.join(loaded)}")
    if not any(isinstance(finder, _RefuseTradingClient) for finder in sys.meta_path):
        sys.meta_path.insert(0, _RefuseTradingClient())


# --- reading -----------------------------------------------------------------

def default_ticket_dir() -> Path:
    """~/.tradingagents/tickets, beside the status files and out of git."""
    return Path.home() / ".tradingagents" / "tickets"


def latest_blackout(as_of: date, status_dir: str | Path | None = None) -> tuple[Path, dict]:
    """The newest earnings blackout for as_of, by the created_at inside each file."""
    folder = Path(status_dir) if status_dir is not None else default_status_dir()
    found = []
    for path in sorted(folder.glob(f"earnings_blackout_{as_of}_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(data["created_at"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TicketInputError(f"cannot read earnings blackout {path}: {exc}") from exc
        if data.get("as_of") == as_of.isoformat():
            found.append((created, path, data))
    if not found:
        raise TicketInputError(f"no earnings blackout for {as_of} in {folder}")
    _, path, data = max(found, key=lambda item: item[0])
    if data.get("report_version") != BLACKOUT_REPORT_VERSION:
        raise TicketInputError(f"earnings blackout {path} has unsupported report_version {data.get('report_version')!r}")
    allowed = data.get("allowed")
    if not isinstance(allowed, dict) or not all(isinstance(allowed.get(side), list) for side in SIDES):
        raise TicketInputError(f"earnings blackout {path} has no allowed long and short lists")
    return path, data


def load_handoff(status: dict, handoff_dir: str | Path | None = None) -> tuple[Path, dict]:
    """The ranked side handoff the daily status used, found by file name in handoff_dir."""
    recorded = (status.get("sources") or {}).get("ranked_side_handoff")
    if not recorded:
        raise TicketInputError("the daily status names no ranked side handoff")
    folder = Path(handoff_dir) if handoff_dir is not None else default_handoff_dir()
    path = folder / Path(recorded).name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TicketInputError(f"cannot read ranked side handoff {path}: {exc}") from exc
    if data.get("as_of") != status.get("as_of"):
        raise TicketInputError(f"ranked side handoff {path} is for {data.get('as_of')}, not {status.get('as_of')}")
    return path, data


# --- tickets -----------------------------------------------------------------

@dataclass(frozen=True)
class Ticket:
    symbol: str
    side: str  # long or short
    order_side: str  # buy or sell
    entry: float
    stop: float
    target: float
    quantity: int
    line: str  # support or resistance
    line_level: float
    atr_14: float
    midline: float
    risk_per_share: float
    reward_per_share: float
    reward_risk: float
    risk_dollars: float
    notional: float
    risk_limit_dollars: float
    notional_limit_dollars: float
    quantity_by_risk: int
    quantity_by_notional: int
    size_bound_by: str  # risk or notional
    earnings_result: str | None  # from the blackout: clear, flagged_kept, unknown_kept
    paper: bool = True


def build_ticket(symbol: str, side: str, levels: dict, earnings_result: str | None = None,
                 account_equity: float = ACCOUNT_EQUITY) -> Ticket:
    """One ticket from the handoff's recorded levels. Raises ValueError when they cannot make one."""
    line_word = LINE[side]
    line, atr, mid = levels.get(line_word), levels.get("atr_14"), levels.get("midline")
    if line is None or mid is None or atr is None or not atr > 0:
        raise ValueError(f"no usable {line_word}, atr_14 or midline (got {line!r}, {atr!r}, {mid!r})")
    sign = 1 if side == LONG else -1
    entry = round(line, 2)
    stop = round(line - sign * STOP_ATR * atr, 2)
    target = round(mid, 2)
    risk_per_share = round(sign * (entry - stop), 2)
    reward_per_share = round(sign * (target - entry), 2)
    if not risk_per_share > 0:
        raise ValueError(f"stop {stop} is not beyond entry {entry}")
    if not reward_per_share > 0:
        raise ValueError(f"midline {target} is not on the profit side of entry {entry}")

    risk_limit = round(account_equity * RISK_PCT, 2)
    notional_limit = round(account_equity * NOTIONAL_PCT, 2)
    by_risk = math.floor(risk_limit / risk_per_share)
    by_notional = math.floor(notional_limit / entry)
    quantity = min(by_risk, by_notional)
    if quantity < 1:
        raise ValueError(f"no whole share fits: {by_risk} by risk, {by_notional} by notional")
    return Ticket(
        symbol=symbol,
        side=side,
        order_side="buy" if side == LONG else "sell",
        entry=entry,
        stop=stop,
        target=target,
        quantity=quantity,
        line=line_word,
        line_level=line,
        atr_14=atr,
        midline=mid,
        risk_per_share=risk_per_share,
        reward_per_share=reward_per_share,
        reward_risk=round(reward_per_share / risk_per_share, 2),
        risk_dollars=round(quantity * risk_per_share, 2),
        notional=round(quantity * entry, 2),
        risk_limit_dollars=risk_limit,
        notional_limit_dollars=notional_limit,
        quantity_by_risk=by_risk,
        quantity_by_notional=by_notional,
        size_bound_by=RISK if by_risk <= by_notional else NOTIONAL,
        earnings_result=earnings_result,
    )


@dataclass(frozen=True)
class PaperTickets:
    as_of: date
    created_at: datetime
    market_label: str
    channel_names_blocked: bool
    tickets: list[Ticket]
    skipped: dict[str, str]  # symbol -> why no ticket
    no_ticket_reason: str | None  # set when the day has no tickets at all
    sources: dict[str, str | None]
    rules: dict = field(default_factory=lambda: {
        "account_equity_placeholder": ACCOUNT_EQUITY,
        "entry": "the channel line: support for long, resistance for short",
        "stop_atr_beyond_line": STOP_ATR,
        "target": "the channel midline",
        "risk_pct": RISK_PCT,
        "notional_pct": NOTIONAL_PCT,
        "quantity": "min(floor(risk limit / risk per share), floor(notional limit / entry))",
        "names": "the earnings blackout's allowed long and short names",
    })
    warnings: list[str] = field(default_factory=list)
    paper: bool = True
    orders_placed: bool = False
    ticket_version: int = TICKET_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data


def build_tickets(
    as_of: date,
    status: tuple[Path, dict],
    blackout: tuple[Path, dict] | None = None,
    handoff: tuple[Path, dict] | None = None,
    created_at: datetime | None = None,
) -> PaperTickets:
    """Tickets for the allowed names. Under any label but SIDE the blackout and handoff are not needed. Writes nothing."""
    status_path, s = status
    label = s.get("market_label", "")
    created = created_at or datetime.now(UTC)
    sources = {"daily_status": str(status_path), "earnings_blackout": None, "ranked_side_handoff": None}

    def no_tickets(reason: str) -> PaperTickets:
        return PaperTickets(as_of, created, label, bool(s.get("channel_names_blocked")), [], {}, reason, sources)

    if label != SIDE:
        return no_tickets(f"market label is {label}, not SIDE; no tickets")
    if s.get("channel_names_blocked"):
        return no_tickets("the daily status blocks the channel names; no tickets")
    if blackout is None or handoff is None:
        raise TicketInputError("a SIDE day needs the earnings blackout and the ranked side handoff")

    b_path, b = blackout
    h_path, h = handoff
    sources.update(earnings_blackout=str(b_path), ranked_side_handoff=str(h_path))
    if Path(b.get("daily_status_file") or "").name != status_path.name:
        raise TicketInputError(f"the earnings blackout {b_path.name} was built from {b.get('daily_status_file')}, "
                               f"not the newest daily status {status_path.name}")
    if b.get("market_label") != label:
        raise TicketInputError(f"the earnings blackout records label {b.get('market_label')}, the daily status {label}")
    for side in SIDES:
        unconfirmed = [n for n in b["allowed"][side] if n not in s["confirmed"][side]]
        if unconfirmed:
            raise TicketInputError(f"allowed {side} names not confirmed in the daily status: {', '.join(unconfirmed)}")

    results = {c["symbol"]: c.get("result") for c in b.get("names", [])}
    names = h.get("names", {})
    tickets: list[Ticket] = []
    skipped: dict[str, str] = {}
    for side in SIDES:
        for symbol in b["allowed"][side]:
            entry = names.get(symbol)
            if entry is None or entry.get("side") != side:
                skipped[symbol] = f"not a {side} name in the ranked side handoff"
                continue
            try:
                tickets.append(build_ticket(symbol, side, entry.get("levels") or {}, results.get(symbol)))
            except ValueError as exc:
                skipped[symbol] = str(exc)

    warnings = [f"{sym}: no ticket, {why}" for sym, why in skipped.items()]
    warnings += [f"{t.symbol}: earnings result {t.earnings_result}" for t in tickets
                 if t.earnings_result not in (None, "clear")]
    warnings += list(b.get("warnings", []))
    return PaperTickets(
        as_of=as_of,
        created_at=created,
        market_label=label,
        channel_names_blocked=False,
        tickets=tickets,
        skipped=skipped,
        no_ticket_reason=None if tickets else "no allowed name made a ticket",
        sources=sources,
        warnings=list(dict.fromkeys(warnings)),
    )


def tickets_for_date(
    as_of: date,
    status_dir: str | Path | None = None,
    handoff_dir: str | Path | None = None,
) -> PaperTickets:
    try:
        status = latest_daily_status(as_of, status_dir)
    except BlackoutInputError as exc:
        raise TicketInputError(str(exc)) from exc
    if status[1].get("market_label") != SIDE or status[1].get("channel_names_blocked"):
        return build_tickets(as_of, status)
    return build_tickets(as_of, status, latest_blackout(as_of, status_dir), load_handoff(status[1], handoff_dir))


def save_tickets(t: PaperTickets, out_dir: str | Path | None = None) -> Path:
    """Write the tickets as a new file and return its path. An existing file is never overwritten."""
    if not t.tickets:
        raise ValueError(f"no tickets to write: {t.no_ticket_reason}")
    folder = Path(out_dir) if out_dir is not None else default_ticket_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"paper_tickets_{t.as_of}_{t.created_at:%Y%m%dT%H%M%SZ}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(t.to_dict(), fh, indent=2)
        fh.write("\n")
    return path
