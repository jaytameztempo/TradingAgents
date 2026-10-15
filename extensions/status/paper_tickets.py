"""Paper-order tickets for the SIDE names still allowed after the earnings blackout. It places no orders.

It reads five saved files for one date:

- the newest daily status (the market label and the ranked side handoff it used),
- the newest earnings blackout, which must be built from that daily status
  (its ``allowed`` long and short names are the only names ticketed),
- the ranked side handoff named in the daily status (each name's rank, side and
  ``midline``),
- the newest long and short channel reports for the date, by the created_at
  inside each file, not the ones the handoff names (each name's membership,
  ``support`` or ``resistance``, daily ``atr_14`` and ``stop_swing_low`` or
  ``stop_swing_high``, as the scan measured them).
  The newest report must carry the ``pivot_confirmed`` stage; if it does not,
  there are no tickets, and an older confirmed report is never used instead. A
  name that is not a passing member of that report gets no ticket.

Equity and buying power are not set here. On a day with tickets the caller
reads them from the Alpaca paper account at ticket time and passes them in as
an ``Account`` (run_paper_tickets does a read-only GET /v2/account). This
module makes no request itself.

Under any label but SIDE, or when the daily status blocks the channel names,
there are no tickets: only the channel names have lines to ticket. Each name
that survived the earnings blackout (4-hour confirmed names, long and short)
is ticketed when it has usable levels; the TTM Squeeze gate (``squeeze_gate``)
is not run here. N is the number of names that pass the blackout and have
usable levels. Each ticket:

- entry: a limit at the channel level from the channel report, support from the
  long channel report for a long (limit buy), resistance from the short channel
  report for a short (limit sell), rounded to the cent; never the last close,
- channel_level: that level as the report recorded it,
- stop_swing: of the confirmed ZigZag swings whose extreme bar is inside the
  120 sessions ending on as_of (all the bars when there are fewer), for a long
  the highest swing low below support that has another swing low in the window
  within 1 ATR(14) of it (inclusive; the other one need not be below support);
  for a short the lowest swing high above resistance with another swing high
  within 1 ATR(14). A swing more than 3 ATR(14) beyond the line does not count.
  The channel report records it (``stop_swing_low`` / ``stop_swing_high``, rule
  ``clustered_within_3atr_beyond_line``); a name with no such
  swing, or a report without the field or recorded under an older rule, gets no
  ticket. The channel level is never used in its place,
- stop_calculated: stop_swing minus ``STOP_ATR`` (1) x the report's atr_14
  for a long, plus it for a short,
- stop: stop_calculated snapped away from the entry to a price ending in .99 or
  .49 at or below it (long), .01 or .51 at or above it (short), see
  ``snap_stop``,
- stop_offset: the stop's distance from the entry, which the sender places
  from the real fill (``stop_for_fill``),
- target: the channel midline,
- quantity: floor(name notional limit / entry) whole shares.

Size is not set by the stop distance. Every name gets the same dollars:
buying power / N, capped at ``NAME_NOTIONAL_PCT`` (15%) of buying power
(``name_notional_limit``); no name is shrunk or skipped because another used
the budget. A name is skipped, with the reason, when it is in the earnings
blackout, is not a member of the newest confirmed channel report, has no usable
levels, or when not one share fits its dollars. Tickets
are listed in rank order, long and short interleaved (long #1, short #1,
long #2, ...).

Every file is read with plain ``json``; no Alpaca or paper_orders module is
loaded here, and ``refuse_trading_client`` makes any later import of
``alpaca.trading`` fail.
The scans, router, bots, daily status and blackout are not touched.
"""

from __future__ import annotations

import importlib.abc
import json
import math
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from extensions.status.daily_status import LONG, SHORT, SIDE, SIDES, default_handoff_dir, default_status_dir
from extensions.status.earnings_blackout import BlackoutInputError, latest_daily_status

TICKET_VERSION = 11  # 2: stop_offset from the fill; 3: equal risk a name; 4: snapped pivot -/+ 1 ATR stop, buying power / N;
# 5: entry and pivot are the channel report's support (long) or resistance (short); 6: 4h + 1h TTM Squeeze gate;
# 7: the TTM Squeeze gate removed; 8: the newest pivot-confirmed channel report, members only;
# 9: the stop is the 120-session swing extreme -/+ 1 ATR, not the channel level;
# 10: the stop swing is the nearest clustered swing beyond the channel level in 120 sessions, not the extreme;
# 11: a stop swing more than 3 ATR beyond the channel level is discarded
BLACKOUT_REPORT_VERSION = 1
CHANNEL_REPORT_VERSION = 1
CONFIRMED_STAGE = "pivot_confirmed"  # the stage_counts key a confirmed channel report carries
NAME_NOTIONAL_PCT = 0.15  # of buying power, the most any one name gets
STOP_ATR = 1.0  # beyond the channel level
STOP_ENDINGS = {LONG: (49, 99), SHORT: (1, 51)}  # cents a snapped stop ends in
LINE = {LONG: "support", SHORT: "resistance"}
STOP_SWING = {LONG: "stop_swing_low", SHORT: "stop_swing_high"}  # recorded by the channel scans
STOP_SWING_RULE = "clustered_within_3atr_beyond_line"  # the rule the channel scans record with the stop swing
SWING_WORD = {LONG: "low", SHORT: "high"}
BEYOND = {LONG: "below support", SHORT: "above resistance"}
CHANNEL_REPORT = {LONG: "long_channel_report", SHORT: "short_channel_report"}
CHANNEL_FILE = {LONG: "long_channel", SHORT: "short_channel"}
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


def default_scan_dir() -> Path:
    """~/.tradingagents/scanbot, where the channel reports are saved."""
    return Path.home() / ".tradingagents" / "scanbot"


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


def latest_channel_report(as_of: date, side: str, scan_dir: str | Path | None = None) -> tuple[Path, dict]:
    """The newest long or short channel report for as_of, by the created_at inside each file (not the file name, not
    the report the handoff names). It must carry the pivot confirmation stage; an older confirmed report is never
    used in its place. A file that cannot be read stops the run rather than being passed over."""
    folder = Path(scan_dir) if scan_dir is not None else default_scan_dir()
    found = []
    for path in sorted(folder.glob(f"{CHANNEL_FILE[side]}_{as_of}_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(data["created_at"])
            if created.tzinfo is None:
                raise ValueError(f"created_at {data['created_at']!r} has no time zone")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TicketInputError(f"cannot read {side} channel report {path}: {exc}") from exc
        if data.get("as_of") == as_of.isoformat():
            found.append((created, path, data))
    if not found:
        raise TicketInputError(f"no {side} channel report for {as_of} in {folder}")
    _, path, data = max(found, key=lambda item: item[0])
    if data.get("report_version") != CHANNEL_REPORT_VERSION:
        raise TicketInputError(f"{side} channel report {path} has unsupported report_version "
                               f"{data.get('report_version')!r}")
    if CONFIRMED_STAGE not in (data.get("stage_counts") or {}):
        raise TicketInputError(f"the newest {side} channel report {path} has no {CONFIRMED_STAGE} stage; "
                               "rerun the scan with pivot confirmation")
    if not isinstance(data.get("calls"), dict) or not isinstance(data.get("members"), list):
        raise TicketInputError(f"{side} channel report {path} has no calls or members")
    return path, data


def channel_members(report: dict) -> set[str]:
    """The report's members whose call passed: the only names on that side that can be ticketed."""
    calls = report.get("calls") or {}
    return {s for s in report.get("members") or () if (calls.get(s) or {}).get("passed") is True}


def channel_levels(report: dict, symbol: str, side: str) -> tuple[float | None, float | None, dict | None]:
    """The name's channel level (support for a long, resistance for a short), atr_14 and stop swing (stop_swing_low
    for a long, stop_swing_high for a short) from the channel report."""
    metrics = ((report.get("calls") or {}).get(symbol) or {}).get("metrics") or {}
    return metrics.get(LINE[side]), metrics.get("atr_14"), metrics.get(STOP_SWING[side])


# --- the account -------------------------------------------------------------

@dataclass(frozen=True)
class Account:
    """Equity and buying power read from the Alpaca paper account at ticket time."""
    equity: float
    buying_power: float
    read_at: datetime
    source: str
    status: str | None = None


def account_from_alpaca(raw: dict, read_at: datetime, source: str) -> Account:
    """An Account from a GET /v2/account body. Raises TicketInputError unless equity and buying power are above zero."""
    values = {}
    for key in ("equity", "buying_power"):
        try:
            values[key] = float(raw.get(key))
        except (TypeError, ValueError):
            raise TicketInputError(f"the paper account has no usable {key} (got {raw.get(key)!r})") from None
        if not values[key] > 0:
            raise TicketInputError(f"the paper account {key} is {values[key]}, not above zero")
    return Account(values["equity"], values["buying_power"], read_at, source, raw.get("status"))


def name_notional_limit(buying_power: float, n: int) -> float:
    """Per-name dollars: buying power / N, at most NAME_NOTIONAL_PCT of buying power, rounded down to the cent."""
    if n < 1:
        raise ValueError("N must be at least 1")
    return math.floor(round(min(buying_power / n, buying_power * NAME_NOTIONAL_PCT) * 100, 6)) / 100


# --- tickets -----------------------------------------------------------------

def stop_for_fill(side: str, stop_offset: float, fill: float) -> float:
    """The stop for a fill: stop_offset below a long fill, above a short fill. Raises ValueError otherwise."""
    if side not in (LONG, SHORT):
        raise ValueError(f"side {side!r} is not {LONG} or {SHORT}")
    if not stop_offset > 0 or not fill > 0:
        raise ValueError(f"stop offset {stop_offset!r} and fill {fill!r} must both be above zero")
    sign = 1 if side == LONG else -1
    stop = round(fill - sign * stop_offset, 2)
    if not stop > 0 or not sign * (fill - stop) > 0:
        raise ValueError(f"{side} stop {stop} is not beyond fill {fill}")
    return stop


def snap_stop(side: str, level: float) -> float:
    """The nearest price ending in .49 or .99 at or below level (long), .01 or .51 at or above it (short)."""
    endings = STOP_ENDINGS[side]  # the two endings are 50 cents apart, so one repeats every 50 cents
    if side == LONG:
        cents = math.floor(round(level * 100, 6))
        return (cents - (cents - endings[0]) % 50) / 100
    cents = math.ceil(round(level * 100, 6))
    return (cents + (endings[0] - cents) % 50) / 100


@dataclass(frozen=True)
class Ticket:
    symbol: str
    side: str  # long or short
    order_side: str  # buy or sell
    entry: float
    stop: float  # stop_calculated snapped to .99/.49 (long) or .01/.51 (short)
    stop_offset: float  # the stop's distance from the entry
    target: float
    quantity: int
    line: str  # support or resistance
    channel_level: float  # from the channel report, unrounded: the limit entry's level
    atr_14: float  # from the channel report
    midline: float
    stop_swing: float  # the clustered swing low (long) or high (short) from the channel report: the stop's pivot
    stop_swing_date: str | None  # the swing's extreme bar
    stop_calculated: float  # stop_swing -/+ STOP_ATR x atr_14, before the snap
    risk_per_share: float
    reward_per_share: float
    reward_risk: float
    risk_dollars: float
    notional: float
    notional_limit_dollars: float  # buying power / N, capped at NAME_NOTIONAL_PCT of buying power
    earnings_result: str | None  # from the blackout: clear, flagged_kept, unknown_kept
    rank: int | None = None  # the handoff's rank within its side
    paper: bool = True


def _price(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def plan_ticket(symbol: str, side: str, line: float | None, atr: float | None, midline: float | None,
                swing: dict | None) -> dict:
    """Entry, stop and target before sizing: a limit entry at the channel line (support for a long, resistance for a
    short, from the channel report), the stop the report's clustered swing low below support (long) or swing high
    above resistance (short) -/+ STOP_ATR x the report's atr_14, snapped. Raises ValueError when they cannot make a
    ticket."""
    line_word, mid = LINE[side], midline
    if not _price(line) or not _price(atr) or not _price(mid):
        raise ValueError(f"no usable {line_word} or atr_14 in the {side} channel report, or midline in the handoff "
                         f"(got {line!r}, {atr!r}, {mid!r})")
    if not isinstance(swing, dict):
        raise ValueError(f"no {STOP_SWING[side]} in the {side} channel report; rerun the {side} channel scan")
    if swing.get("rule") != STOP_SWING_RULE:
        raise ValueError(f"the {STOP_SWING[side]} in the {side} channel report is from an older rule; rerun the {side} "
                         f"channel scan")
    swing_price = swing.get("price")
    if not _price(swing_price):
        word = SWING_WORD[side]
        raise ValueError(f"no confirmed swing {word} {BEYOND[side]} by at most 3 ATR with another swing {word} within "
                         f"1 ATR in the last {swing.get('window_sessions')} sessions in the {side} channel report")
    sign = 1 if side == LONG else -1
    entry = round(line, 2)
    calculated = round(swing_price - sign * STOP_ATR * atr, 4)
    stop = snap_stop(side, calculated)
    if not stop > 0:
        raise ValueError(f"stop {stop} from swing {SWING_WORD[side]} {swing_price} {'-' if side == LONG else '+'} "
                         f"{STOP_ATR:g} x atr_14 {atr} is not above zero")
    stop_offset = round(sign * (entry - stop), 2)
    if not stop_offset > 0:
        raise ValueError(f"stop {stop} is not beyond entry {entry}")
    target = round(mid, 2)
    reward_per_share = round(sign * (target - entry), 2)
    if not reward_per_share > 0:
        raise ValueError(f"midline {target} is not on the profit side of entry {entry}")
    return {"entry": entry, "stop": stop, "stop_offset": stop_offset, "target": target, "line": line_word,
            "channel_level": line, "atr_14": atr, "midline": mid, "stop_swing": swing_price,
            "stop_swing_date": swing.get("date"), "stop_calculated": calculated, "risk_per_share": stop_offset,
            "reward_per_share": reward_per_share}


def size_ticket(symbol: str, side: str, plan: dict, earnings_result: str | None = None, *, notional_limit: float,
                rank: int | None = None) -> Ticket:
    """A ticket of floor(notional limit / entry) whole shares. Raises ValueError when not one share fits."""
    risk_per_share = plan["risk_per_share"]
    quantity = math.floor(round(notional_limit / plan["entry"], 6))
    if quantity < 1:
        raise ValueError(f"no whole share fits: ${notional_limit:,.2f} a name at entry ${plan['entry']:,.2f}")
    return Ticket(
        symbol=symbol,
        side=side,
        order_side="buy" if side == LONG else "sell",
        quantity=quantity,
        **plan,
        reward_risk=round(plan["reward_per_share"] / risk_per_share, 2),
        risk_dollars=round(quantity * risk_per_share, 2),
        notional=round(quantity * plan["entry"], 2),
        notional_limit_dollars=notional_limit,
        earnings_result=earnings_result,
        rank=rank,
    )


def build_ticket(symbol: str, side: str, line: float | None, atr: float | None, midline: float | None,
                 swing: dict | None, earnings_result: str | None = None, *, notional_limit: float,
                 rank: int | None = None) -> Ticket:
    """One ticket from the channel report's line, atr_14 and stop swing and the handoff's midline. Raises ValueError
    when they cannot make one."""
    return size_ticket(symbol, side, plan_ticket(symbol, side, line, atr, midline, swing), earnings_result,
                       notional_limit=notional_limit, rank=rank)


def blackout_reason(check: dict) -> str:
    """Why the blackout removed a name, from its name check."""
    if check.get("earnings_date") is None:
        return "in the earnings blackout"
    source = f" ({check['date_source']})" if check.get("date_source") else ""
    return (f"in the earnings blackout: earnings {check['earnings_date']}{source}, "
            f"{check.get('sessions_away')} sessions away")


def _rank_order(allowed: dict[str, list[str]], names: dict) -> list[tuple[str, str]]:
    """(symbol, side) by handoff rank, long before short on a tie; a name with no rank goes last on its side."""
    order = []
    for side_order, side in enumerate(SIDES):
        for pos, symbol in enumerate(allowed[side]):
            rank = (names.get(symbol) or {}).get("rank")
            key = (rank if isinstance(rank, int) else math.inf, side_order, pos)
            order.append((key, symbol, side))
    return [(symbol, side) for _, symbol, side in sorted(order)]


def _rules(account: Account | None = None, n: int | None = None, notional_limit: float | None = None) -> dict:
    return {
        "account_equity": account.equity if account else None,
        "buying_power": account.buying_power if account else None,
        "account_source": account.source if account else None,
        "account_read_at": account.read_at.isoformat() if account else None,
        "names_n": n,
        "names": "the earnings blackout's allowed long and short names that are passing members of the channel report "
                 "and have usable levels",
        "channel_report": f"the newest {{long|short}}_channel report for the date by the created_at inside the file; "
                          f"it must carry the {CONFIRMED_STAGE} stage, else no tickets",
        "name_notional": "buying power / N, capped at name_notional_pct_of_buying_power, rounded down to the cent, "
                         "the same for every name",
        "name_notional_pct_of_buying_power": NAME_NOTIONAL_PCT,
        "name_notional_limit": notional_limit,
        "entry": "limit at the channel level, rounded to the cent: limit buy at support from the long channel "
                 "report (long), limit sell at resistance from the short channel report (short); not the last close",
        "stop_swing": "of the confirmed ZigZag swings whose extreme bar is inside the 120 sessions ending on as_of, "
                      "or all the bars when there are fewer: the highest swing low below support that has another "
                      "swing low in the window within 1 x atr_14 of it, inclusive (long); the lowest swing high above "
                      "resistance with another swing high within 1 x atr_14 (short); the other swing need not be "
                      "beyond the line; a swing more than 3 x atr_14 beyond the line does not count; from the channel report (stop_swing_low / stop_swing_high, rule "
                      f"{STOP_SWING_RULE}); no such swing, no ticket",
        "stop_atr_beyond_swing": STOP_ATR,
        "stop": "stop_swing - STOP_ATR x atr_14 snapped down to a price ending .99 or .49 at or below it (long); "
                "stop_swing + STOP_ATR x atr_14 snapped up to a price ending .01 or .51 at or above it (short); "
                "atr_14 from the channel report",
        "target": "the channel midline",
        "quantity": "floor(name notional limit / entry); not sized by the stop distance",
        "order": "rank order, long before short on a tie; listing order does not change any size",
    }


@dataclass(frozen=True)
class PaperTickets:
    as_of: date
    created_at: datetime
    market_label: str
    channel_names_blocked: bool
    tickets: list[Ticket]
    skipped: dict[str, str]  # symbol -> why it made no ticket (blackout, not a channel member, no usable levels, no share fits)
    no_ticket_reason: str | None  # set when the day has no tickets at all
    sources: dict[str, str | None]
    total_notional: float = 0.0
    total_risk: float = 0.0
    rules: dict = field(default_factory=_rules)
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
    account: Account | None = None,
    created_at: datetime | None = None,
    channels: dict[str, tuple[Path, dict]] | None = None,
) -> PaperTickets:
    """Tickets for the allowed names, sized from the account, entries and stops from the channel reports (channels:
    side -> report, for each side with allowed names). Only passing members of a side's channel report are ticketed.
    Under any label but SIDE the blackout, handoff, account and channel reports are not needed. Writes nothing."""
    status_path, s = status
    label = s.get("market_label", "")
    created = created_at or datetime.now(UTC)
    sources = {"daily_status": str(status_path), "earnings_blackout": None, "ranked_side_handoff": None,
               "long_channel_report": None, "short_channel_report": None}

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

    checks = {c.get("symbol"): c for c in b.get("names", []) if isinstance(c, dict)}
    skipped: dict[str, str] = {sym: blackout_reason(checks.get(sym) or {}) for sym in b.get("removed", [])}
    if not any(b["allowed"][side] for side in SIDES):
        return PaperTickets(as_of, created, label, False, [], skipped,
                            "the earnings blackout allows no names; no tickets", sources,
                            warnings=[f"{sym}: no ticket, {why}" for sym, why in skipped.items()]
                            + list(b.get("warnings", [])))
    if account is None:
        raise TicketInputError("a SIDE day needs equity and buying power read from the paper account")
    channels = channels or {}
    ranked_from: list[str] = []
    for side in SIDES:
        if b["allowed"][side] and side not in channels:
            raise TicketInputError(f"allowed {side} names need the {side} channel report for their levels")
        if side in channels:
            sources[CHANNEL_REPORT[side]] = str(channels[side][0])
            recorded = Path(h.get(CHANNEL_REPORT[side]) or "").name
            if recorded != channels[side][0].name:
                ranked_from.append(f"the ranked side handoff ranked from {side} channel report {recorded or 'none'}; "
                                   f"membership and levels are from the newer {channels[side][0].name}")
    members = {side: channel_members(channels[side][1]) for side in channels}

    # first the entries and stops: N is the allowed names that are channel members with usable levels
    names = h.get("names", {})
    plans: list[tuple[str, str, int | None, dict]] = []
    for symbol, side in _rank_order(b["allowed"], names):
        entry = names.get(symbol)
        if entry is None or entry.get("side") != side:
            skipped[symbol] = f"not a {side} name in the ranked side handoff"
            continue
        if symbol not in members[side]:
            skipped[symbol] = f"not a member of the newest confirmed {side} channel report {channels[side][0].name}"
            continue
        try:
            line, atr, swing = channel_levels(channels[side][1], symbol, side)
            plan = plan_ticket(symbol, side, line, atr, (entry.get("levels") or {}).get("midline"), swing)
        except ValueError as exc:
            skipped[symbol] = str(exc)
            continue
        plans.append((symbol, side, entry.get("rank"), plan))

    # then the size: every name gets the same dollars of buying power
    n = len(plans)
    notional_limit = name_notional_limit(account.buying_power, n) if n else None
    tickets: list[Ticket] = []
    for symbol, side, rank, plan in plans:
        try:
            tickets.append(size_ticket(symbol, side, plan, (checks.get(symbol) or {}).get("result"),
                                       notional_limit=notional_limit, rank=rank))
        except ValueError as exc:
            skipped[symbol] = str(exc)
    total_notional = round(sum(t.notional for t in tickets), 2)
    total_risk = round(sum(t.risk_dollars for t in tickets), 2)

    warnings = [f"{sym}: no ticket, {why}" for sym, why in skipped.items()]
    warnings += [f"{t.symbol}: earnings result {t.earnings_result}" for t in tickets
                 if t.earnings_result not in (None, "clear")]
    warnings += list(b.get("warnings", []))
    warnings += ranked_from
    return PaperTickets(
        as_of=as_of,
        created_at=created,
        market_label=label,
        channel_names_blocked=False,
        tickets=tickets,
        skipped=skipped,
        no_ticket_reason=None if tickets else "no allowed name made a ticket",
        sources=sources,
        total_notional=total_notional,
        total_risk=total_risk,
        rules=_rules(account, n, notional_limit),
        warnings=list(dict.fromkeys(warnings)),
    )


def tickets_for_date(
    as_of: date,
    status_dir: str | Path | None = None,
    handoff_dir: str | Path | None = None,
    read_account: Callable[[], Account] | None = None,
    scan_dir: str | Path | None = None,
) -> PaperTickets:
    """Tickets from the saved files. read_account is called only on a SIDE day with allowed names, at ticket time."""
    try:
        status = latest_daily_status(as_of, status_dir)
    except BlackoutInputError as exc:
        raise TicketInputError(str(exc)) from exc
    if status[1].get("market_label") != SIDE or status[1].get("channel_names_blocked"):
        return build_tickets(as_of, status)
    blackout = latest_blackout(as_of, status_dir)
    handoff = load_handoff(status[1], handoff_dir)
    account = None
    channels = {side: latest_channel_report(as_of, side, scan_dir) for side in SIDES if blackout[1]["allowed"][side]}
    if channels:
        if read_account is None:
            raise TicketInputError("a SIDE day needs equity and buying power read from the paper account")
        account = read_account()
    return build_tickets(as_of, status, blackout, handoff, account, channels=channels)


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
