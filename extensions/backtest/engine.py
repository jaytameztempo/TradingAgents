"""Backtest a date range: what would the system have traded, and how would it have done?

Each weekday in the range has one source:
- **replay**: the date has a saved route, so its trades come from the files the pipeline saved;
- **reconstructed**: the date was rebuilt by ``run_backtest_reconstruct`` (see ``reconstruct.py``), so its trades come
  from the rebuilt files under ``backtest/reconstructed``;
- otherwise nothing (the page offers to rebuild it).

For an as-of date A, each kind of file used is the newest one with as-of A written within MAX_REPORT_AGE_DAYS of A
(same-week re-runs count, much later re-runs are ignored). A signal from a file is acted on the day that file was
written (its New York date): the regime label is the newest route visible that day. The trades:
1. SIDEBot: the PLAN names of the side handoff (ranked first), long and short together in rank order (long #1,
   short #1, ...); with no handoff, the pivot-confirmed channel report. Entry: a DAY limit at the channel line.
2. UPBot / DOWNBot: the route's ``up_passed`` / ``down_passed`` (levels from its basket), and the trend handoff.
   Entry: the next session's open.
3. The live regime rule: UPBot only on UP, DOWNBot only on DOWN, SIDEBot only on SIDE.
4. Each file's names are one basket, sized as ``sizing.py`` says (live by default), then held to the guard's limits
   in order; one position a symbol at a time, within each origin. The two origins are never merged.
Levels per ``levels.py``; the trade per ``simulate.py``.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from extensions.backtest import pit, sizing
from extensions.backtest import triggers as tb
from extensions.backtest.bars import DAILY, TIMEFRAMES, BarCache
from extensions.backtest.levels import LONG, RULES, SHORT, LevelsError, Plan, side_plan_from_channel, \
    side_plan_from_handoff, trend_plan_for
from extensions.backtest.simulate import BARS_END, BOTS, NOT_CLOSED, OPEN_AT_END, Skip, breakdown, simulate, \
    summarize
from extensions.status import confirmation
from extensions.status.paper_tickets import _rank_order

STRATEGIES = ("all", *BOTS)
REPLAY, RECONSTRUCTED = "replay", "reconstructed"
ORIGINS = (REPLAY, RECONSTRUCTED)
ORIGIN_CHOICES = {"both": ORIGINS, REPLAY: (REPLAY,), RECONSTRUCTED: (RECONSTRUCTED,)}
BOT_REGIME = {"up": "UP", "down": "DOWN", "side": "SIDE"}
BOT_SIDE = {"up": LONG, "down": SHORT}
MAX_REPORT_AGE_DAYS = 10  # a file counts for its as-of only if written within this many days; routes as labels too
MAX_RANGE_DAYS = 366
MAX_SESSIONS = 60
MIN_CAPITAL, MAX_CAPITAL = 1_000, 1_000_000_000
MAX_LEVERAGE = 4.0  # buying power at most 4 x the capital (Alpaca's intraday margin)
ENTRY_SESSIONS = 1  # a SIDE limit is a DAY order
TRADEBOT, NEXT_DAY = "tradebot", "next_day"
ENTRY_MODES = (TRADEBOT, NEXT_DAY)
BARS_END_SESSIONS = 2  # bars stopping more than this many sessions before the data end: bars_end, not open_at_end

CAVEATS = {
    REPLAY: "Replayed dates use the files the pipeline saved, under the rules of the code that ran then, acting the "
            "session after each file was actually written.",
    RECONSTRUCTED: "Rebuilt dates run today's pipeline code on bars up to the as-of date, acting the next session. "
                   "Not point in time: the universe and borrow flags come from today's asset snapshot "
                   "(survivorship), the spread gate is not applied, fundamentals are today's cached SEC files (facts "
                   "filed by the as-of date; missing ones are downloaded only when SEC download is on), and an "
                   "unmapped sector makes the side handoff unranked.",
}
ASSUMPTIONS = [
    "Starting capital is the equity on every day; there is no compounding. Buying power (default: the starting "
    "capital) stands in for the series' original buying power and the buying power at ticket time: the 23% basket "
    "is a share of it, while the 15% name cap, 8% stop-risk and 60% gross limits are shares of the equity, as live. "
    "Buying power is not reduced by open positions.",
    "Live sizing follows the Tradebot ticketing and the sender's guard (23% basket x calm scale, 5% floor, equal "
    "split capped at 15% a name, 8% book stop-risk, 15 positions, 60% gross). The older daily paper-ticket path "
    "(no 15% cap, 8% per basket only) is not what is modelled.",
    "The guard's daily-loss and drawdown stops are not simulated; the book's stop-risk uses each position's risk "
    "at its fill, not the current price.",
    "The live daily confirmation filters and the earnings blackout are not applied: every PLAN name is a ticket.",
    "UP and DOWN enter at the next session's open; the Tradebot's trend tickets use a limit at min(close, SMA20) "
    "(max for a short).",
    "One basket per source file and run date shares one allocation (with Tradebot triggers: the names triggered "
    "on one day).",
    "Tradebot triggers: a SIDE name is queued on its run date and checked each session's close (daily confirmation, "
    "then the 4-hour/1-hour squeeze); an intraday trigger is taken at that close, so its DAY limit goes out the next "
    "session. The earnings gate is not simulated. A name with no cached 30-minute bars has the squeeze taken as off.",
]


class BacktestInputError(ValueError):
    """The request cannot run (bad dates, strategy, timeframe or numbers)."""


@dataclass(frozen=True)
class Config:
    mode: str = sizing.LIVE
    risk_pct: float = 1.0
    capital: float = 100_000.0
    buying_power: float | None = None  # None: the capital
    limits: sizing.Limits = sizing.Limits()
    slippage_bps: float = 0.0
    commission_per_share: float = 0.0
    collect_only: bool = False  # list the symbols that would be simulated, simulate nothing
    entry_mode: str = NEXT_DAY  # SIDE: tradebot (queue, confirm, trigger) or next_day (limit the next session)
    filters: dict = field(default_factory=lambda: dict(tb.DEFAULT_FILTERS))
    expire_sessions: int = tb.DEFAULT_EXPIRE


def _check(start: date, end: date, strategy: str, timeframe: str, max_sessions: int | None,
           risk_pct: float, capital: float = 100_000.0) -> None:
    if end < start:
        raise BacktestInputError("end date is before start date")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise BacktestInputError(f"date range is limited to {MAX_RANGE_DAYS} days")
    if strategy not in STRATEGIES:
        raise BacktestInputError(f"strategy must be one of {', '.join(STRATEGIES)}")
    if timeframe not in TIMEFRAMES:
        raise BacktestInputError(f"bars must be one of {', '.join(TIMEFRAMES)}")
    if max_sessions is not None and not 1 <= max_sessions <= MAX_SESSIONS:
        raise BacktestInputError(f"max sessions must be 1 to {MAX_SESSIONS}")
    if not 0 < risk_pct <= 5:
        raise BacktestInputError("risk per trade must be above 0 and at most 5 (%)")
    if not MIN_CAPITAL <= capital <= MAX_CAPITAL:
        raise BacktestInputError(f"starting capital must be ${MIN_CAPITAL:,} to ${MAX_CAPITAL:,}")


def _check_more(sizing_mode: str, origins: str, limits: sizing.Limits, slippage_bps: float,
                commission_per_share: float, capital: float = 100_000.0, buying_power: float | None = None) -> None:
    if buying_power is not None and not capital <= buying_power <= capital * MAX_LEVERAGE:
        raise BacktestInputError(f"buying power must be from the starting capital to {MAX_LEVERAGE:g} x it")
    if sizing_mode not in sizing.SIZINGS:
        raise BacktestInputError(f"sizing must be one of {', '.join(sizing.SIZINGS)}")
    if origins not in ORIGIN_CHOICES:
        raise BacktestInputError(f"origins must be one of {', '.join(ORIGIN_CHOICES)}")
    if not 0 <= limits.max_positions <= 200:
        raise BacktestInputError("max open positions must be 0 (no limit) to 200")
    if not 0 <= limits.max_gross_pct <= 400:
        raise BacktestInputError("max gross exposure must be 0 (no limit) to 400 (%)")
    if not 0 <= limits.max_open_risk_pct <= 100:
        raise BacktestInputError("max open stop-risk must be 0 (no limit) to 100 (%)")
    if not 0 <= slippage_bps <= 500:
        raise BacktestInputError("slippage must be 0 to 500 bps")
    if not 0 <= commission_per_share <= 1:
        raise BacktestInputError("commission must be $0 to $1 a share")


def coverage(home: str | Path, start: date, end: date) -> list[dict]:
    """Each weekday in the range: saved (replayed), reconstructed, holiday (found while rebuilding) or missing."""
    if end < start or (end - start).days >= MAX_RANGE_DAYS:
        raise BacktestInputError(f"need start <= end and at most {MAX_RANGE_DAYS} days")
    saved = pit.saved_dates(home)
    rebuilt = pit.read_manifest(home)["dates"]
    rows = []
    for d in pit.weekdays(start, end):
        entry = rebuilt.get(d.isoformat()) or {}
        if d in saved:
            status = "saved"
        elif entry.get("status") == pit.DONE:
            status = "reconstructed"
        elif entry.get("status") == pit.HOLIDAY:
            status = "holiday"
        else:
            status = "missing"
        rows.append({"date": d.isoformat(), "status": status, "label": entry.get("label"),
                     "rebuilt": entry.get("status") == pit.DONE, "warnings": entry.get("warnings", []),
                     "fundamentals": entry.get("fundamentals")})
    return rows


class _Source:
    """One file a signal comes from, and the bot / side it feeds (side "both" for a side handoff)."""

    def __init__(self, bot: str, side: str, saved: pit.Saved, kind: str):
        self.bot, self.side, self.saved, self.kind = bot, side, saved, kind


@dataclass
class _Name:
    """One name of a basket, in rank order."""
    symbol: str
    bot: str
    side: str
    make_plan: object
    note: str = ""
    source: pit.Saved | None = None  # the file it came from, when the basket mixes files
    extra: dict = field(default_factory=dict)


class _Pass:
    """One origin's replay over one data home."""

    def __init__(self, origin: str, home: Path, dates: list[date], bots: tuple[str, ...], cache: BarCache,
                 timeframe: str, max_sessions: int | None, cfg: Config):
        self.origin, self.dates, self.bots, self.cfg = origin, dates, bots, cfg
        self.lib = pit.Library(home)
        self.cache, self.timeframe, self.max_sessions = cache, timeframe, max_sessions
        self.book = sizing.Book(cfg.capital)
        self.trades: list[dict] = []
        self.skipped: list[dict] = []
        self.days: list[dict] = []
        self.symbols: set[str] = set()
        self.calm_fallback: set[str] = set()
        self._index_daily: pd.DataFrame | None = None
        self.queue: list[tb.Item] = []
        self.funnel_notes: Counter = Counter()

    def run(self) -> None:
        sources: list[_Source] = []
        for as_of in self.dates:
            found = self._sources(as_of)
            sources += found
            route = self._pick("route", as_of)
            self.days.append({
                "date": as_of.isoformat(), "origin": self.origin,
                "regime_label": str(route.data.get("regime_label")) if route else None,
                "route_file": route.path.name if route else None,
                "sources": [{"bot": s.bot, "side": s.side, "file": s.saved.path.name,
                             "known_on": s.saved.known_on.isoformat()} for s in found],
            })
        # Act in the order the files were written, so the book runs forward in time.
        sources.sort(key=lambda s: (s.saved.known_on, s.saved.created_at, s.bot, s.side))
        if self.cfg.entry_mode != TRADEBOT or self.cfg.collect_only:
            for source in sources:
                self._act(source)
            return
        # Tradebot triggers: SIDE names go through the queue; their baskets are the names triggered on one day.
        others = [s for s in sources if s.bot != "side"]
        events = [(s.saved.known_on, 1, i, ("act", s)) for i, s in enumerate(others)]
        for day, names in self._tradebot([s for s in sources if s.bot == "side"]):
            events.append((day, 0, len(events), ("basket", day, names)))
        events.sort(key=lambda e: e[:3])
        for _, _, _, event in events:
            if event[0] == "act":
                self._act(event[1])
            else:
                self._basket(event[1], None, "SIDE", event[2])

    # --- the Tradebot queue ------------------------------------------------------------------

    def _queue_names(self, source: _Source) -> list[tuple[str, str]]:
        """(symbol, side) a side file queues, in rank order."""
        data = source.saved.data
        if source.kind == "handoff":
            entries = data.get("names") or {}
            allowed = {LONG: [], SHORT: []}
            for symbol, entry in entries.items():
                if entry.get("status") == "PLAN":
                    allowed[_handoff_side(entry)].append(symbol)
            return _rank_order(allowed, entries)
        return [(s, source.side) for s in data.get("members") or []]

    def _label_on(self, day: date) -> str | None:
        route = self.lib.newest("route", day)
        if route is None or (day - route.as_of).days > MAX_REPORT_AGE_DAYS:
            return None
        return str(route.data.get("regime_label") or "") or None

    def _member_on(self, symbol: str, side: str, day: date) -> tuple[bool, str]:
        """Live's membership gate: the name passes the newest pivot-confirmed channel report of its side."""
        kind = "long_channel" if side == LONG else "short_channel"
        report = self.lib.newest(kind, day, pit.pivot_confirmed)
        if report is None:
            return True, "no channel report to check"  # nothing newer than the queuing file
        calls = report.data.get("calls") or {}
        if symbol in (report.data.get("members") or []) and (calls.get(symbol) or {}).get("passed") is not False:
            return True, report.path.name
        return False, f"{symbol} is not in the newest {side} channel report {report.path.name}"

    def _plan_on(self, symbol: str, side: str, day: date, fallback: pit.Saved):
        """The levels at the trigger: the newest side handoff on or before ``day`` naming it as a PLAN."""
        visible = [s for s in self.lib.files("side_handoff") if s.known_on <= day]
        visible.sort(key=lambda s: (s.as_of, -s.rank, s.created_at), reverse=True)
        for saved in [*visible, fallback]:
            try:
                entry = (saved.data.get("names") or {}).get(symbol)
            except (OSError, ValueError):
                continue
            if entry and entry.get("status") == "PLAN" and _handoff_side(entry) == side:
                report = self.lib.referenced("scanbot", saved.data.get(f"{side}_channel_report"), day)
                return saved, (lambda s=symbol, e=entry, r=report: side_plan_from_handoff(s, side, e, r and r.data))
        if "members" in (fallback.data or {}):
            return fallback, (lambda: side_plan_from_channel(symbol, side, fallback.data))
        return fallback, None

    def _tradebot(self, side_sources: list[_Source]) -> list[tuple[date, list[_Name]]]:
        bars30 = tb.Bars30(self.cache.folder.parent.parent)
        sessions = lambda s: list(self._daily(s)["session"])  # noqa: E731
        last_end: dict[str, date] = {}
        by_day: dict[date, list[_Name]] = {}
        for source in side_sources:
            run_date = source.saved.known_on
            label = self._label_on(run_date)
            for symbol, side in self._queue_names(source):
                if label != "SIDE":
                    self._skip(run_date, "side", symbol, source.saved,
                               f"regime {label or 'none'} does not match SIDE" if label else
                               "no route visible on this date: no regime label")
                    continue
                if last_end.get(symbol) is not None and last_end[symbol] >= run_date:
                    self.funnel_notes["already queued"] += 1
                    continue
                item = tb.Item(symbol, side, run_date, source.saved)
                tb.run_queue([item], sessions, self._label_on, self._member_on, self.cache, bars30,
                             self.cfg.filters, self.cfg.expire_sessions)
                self.queue.append(item)
                last_end[symbol] = item.ended_on or run_date
                if item.status != tb.TRIGGERED:
                    self._skip(run_date, "side", symbol, source.saved, item.reason)
                    continue
                saved, make_plan = self._plan_on(symbol, side, item.triggered_on, source.saved)
                if make_plan is None:
                    self._skip(item.triggered_on, "side", symbol, saved, "triggered, but no levels on the trigger day")
                    continue
                by_day.setdefault(item.triggered_on, []).append(_Name(
                    symbol, "side", side, make_plan, note=f"queued {run_date}, triggered {item.triggered_on}: "
                                                        f"{item.reason}",
                    source=saved, extra={"queued_on": run_date.isoformat(), "triggered_on":
                                         item.triggered_on.isoformat(), "squeeze_missing": item.squeeze_missing}))
        return sorted(by_day.items())

    def funnel(self) -> dict:
        q = self.queue
        return {"queued": len(q), "armed": sum(i.armed_ever for i in q),
                "triggered": sum(i.status == tb.TRIGGERED for i in q),
                "expired": sum(i.status == tb.EXPIRED for i in q), "removed": sum(i.status == tb.REMOVED for i in q),
                "already_queued": self.funnel_notes["already queued"],
                "squeeze_missing": sum(i.squeeze_missing for i in q),
                "reasons": Counter(_reason_key(i.reason) for i in q if i.status != tb.TRIGGERED).most_common(8)}

    # --- which files -----------------------------------------------------------------------

    def _pick(self, kind: str, as_of: date, accept=None) -> pit.Saved | None:
        """The newest file of ``kind`` for ``as_of`` written within MAX_REPORT_AGE_DAYS (ranked kinds first)."""
        limit = as_of + timedelta(days=MAX_REPORT_AGE_DAYS)
        found = [s for s in self.lib.files(kind) if s.as_of == as_of and s.known_on <= limit]
        found.sort(key=lambda s: (-s.rank, s.created_at), reverse=True)
        for saved in found:
            try:
                if accept is None or accept(saved):
                    return saved
            except (OSError, ValueError) as exc:
                self.lib._warn(f"{saved.path.name} could not be read ({exc}); passed over")
        return None

    def _sources(self, as_of: date) -> list[_Source]:
        out: list[_Source] = []
        if "side" in self.bots:
            handoff = self._pick("side_handoff", as_of)
            if handoff is not None:
                out.append(_Source("side", "both", handoff, "handoff"))
            else:
                for side, kind in ((LONG, "long_channel"), (SHORT, "short_channel")):
                    channel = self._pick(kind, as_of, pit.pivot_confirmed)
                    if channel is not None:
                        out.append(_Source("side", side, channel, "channel"))
        for bot in ("up", "down"):
            if bot not in self.bots:
                continue
            route = self._pick("route", as_of)
            if route is not None:
                out.append(_Source(bot, BOT_SIDE[bot], route, "route"))
            trend = self._pick("trend_handoff", as_of)
            if trend is not None:
                out.append(_Source(bot, BOT_SIDE[bot], trend, "trend_handoff"))
        return out

    # --- one file: its basket --------------------------------------------------------------

    def _act(self, source: _Source) -> None:
        run_date = source.saved.known_on
        route = self.lib.newest("route", run_date)
        if route is not None and (run_date - route.as_of).days > MAX_REPORT_AGE_DAYS:
            route = None
        label = str(route.data.get("regime_label") or "") if route else None
        saved, bot, data = source.saved, source.bot, source.saved.data
        names: list[_Name] = []

        if source.kind == "handoff":
            entries = data.get("names") or {}
            allowed = {LONG: [], SHORT: []}
            for symbol, entry in entries.items():
                if entry.get("status") == "PLAN":
                    allowed[_handoff_side(entry)].append(symbol)
            reports = {side: self.lib.referenced("scanbot", data.get(f"{side}_channel_report"), run_date)
                       for side in (LONG, SHORT)}
            for symbol, side in _rank_order(allowed, entries):
                report = reports[side]
                names.append(_Name(symbol, bot, side, lambda s=symbol, sd=side, e=entries[symbol], r=report:
                                   side_plan_from_handoff(s, sd, e, r and r.data)))
        elif source.kind == "channel":
            side = source.side
            for symbol in data.get("members") or []:
                names.append(_Name(symbol, bot, side, lambda s=symbol: side_plan_from_channel(s, side, data),
                                   "no handoff; channel report levels"))
        elif source.kind == "route":
            side = source.side
            basket = self.lib.referenced("baskets", data.get("basket_file" if bot == "up" else "down_basket_file"),
                                         run_date)
            members = {m.get("ticker"): m.get("metrics") for m in (basket.data.get("members") or [])} if basket else {}
            as_of = basket.as_of if basket else saved.as_of
            for symbol in data.get("up_passed" if bot == "up" else "down_passed") or []:
                names.append(_Name(symbol, bot, side, lambda s=symbol: trend_plan_for(bot, s, side, members.get(s),
                                                                                     self._daily(s), as_of)))
        else:  # trend handoff (4-hour confirmed)
            side = source.side
            for symbol, entry in (data.get("names") or {}).items():
                if entry.get("side") != BOT_REGIME[bot]:
                    continue
                if entry.get("delisted"):
                    self._skip(run_date, bot, symbol, saved, f"delisted after {entry['delisted'].get('after')}")
                    continue
                names.append(_Name(symbol, bot, side, lambda s=symbol: trend_plan_for(bot, s, side, None,
                                                                                     self._daily(s), saved.as_of),
                                   "4-hour confirmed"))
        self._basket(run_date, saved, label, names)

    def _basket(self, run_date: date, source: pit.Saved, label: str | None, names: list[_Name]) -> None:
        planned = []  # (name, plan, bars, planned entry, planned risk per share)
        for n in names:
            src = n.source or source
            if label is None:
                self._skip(run_date, n.bot, n.symbol, src, "no route visible on this date: no regime label")
                continue
            if label != BOT_REGIME[n.bot]:
                self._skip(run_date, n.bot, n.symbol, src, f"regime {label} does not match {BOT_REGIME[n.bot]}")
                continue
            try:
                plan: Plan = n.make_plan()
            except LevelsError as exc:
                self._skip(run_date, n.bot, n.symbol, src, f"no levels: {exc}")
                continue
            if self.cfg.collect_only:
                self.symbols.add(n.symbol)
                continue
            bars = self.cache.bars(n.symbol, self.timeframe)
            if not (bars["session"] > run_date).any():
                word = "4-hour " if self.timeframe != DAILY else ""
                self._skip(run_date, n.bot, n.symbol, src, f"no cached {word}bars after {run_date}")
                continue
            entry, risk = self._planned(plan, run_date)
            if not (entry and risk and risk > 0):
                self._skip(run_date, n.bot, n.symbol, src,
                           f"the close on {run_date} ({entry}) is already past the stop {plan.stop:g}")
                continue
            planned.append((n, plan, bars, entry, risk))
        if not planned:
            return

        qty_by, why_by, info, fallback = {}, {}, {}, False
        if self.cfg.mode == sizing.LIVE:
            scale, readings, fallback = sizing.calm_scale_for(self._indexes(), run_date)
            if fallback:
                self.calm_fallback.add(run_date.isoformat())
            day = min(str(b[b["session"] > run_date]["session"].iloc[0]) for _, _, b, _, _ in planned)
            limit = self.cfg.limits.max_open_risk_pct
            room = self.cfg.capital * limit / 100 - self.book.open_risk(day) if limit else math.inf
            qty_by, why_by, info = sizing.live_basket(
                [{"key": n.symbol, "symbol": n.symbol, "side": n.side, "entry": e, "risk_per_share": r}
                 for n, _, _, e, r in planned], self.cfg.capital, scale, room, self.cfg.buying_power)

        for n, plan, bars, _, _ in planned:
            src = n.source or source
            if n.symbol in why_by:
                self._skip(run_date, n.bot, n.symbol, src, why_by[n.symbol])
                continue
            entry_limit = plan.reference_entry if n.bot == "side" else None
            try:
                trade = simulate(plan, bars, run_date, self.max_sessions, entry_limit=entry_limit,
                                 entry_sessions=ENTRY_SESSIONS, slippage_bps=self.cfg.slippage_bps)
            except Skip as exc:
                word = "4-hour " if self.timeframe != DAILY else ""
                self._skip(run_date, n.bot, n.symbol, src, str(exc).replace("cached bars", f"cached {word}bars"))
                continue
            capped = False
            if self.cfg.mode == sizing.LIVE:
                qty = qty_by[n.symbol]
            else:
                qty, capped = sizing.fixed_quantity(self.cfg.capital, self.cfg.risk_pct, trade["risk_per_share"],
                                                    trade["entry"], self.cfg.buying_power)
                if qty < 1:
                    self._skip(run_date, n.bot, n.symbol, src,
                               f"no whole share fits {self.cfg.risk_pct:g}% risk at {trade['risk_per_share']:g} "
                               "a share")
                    continue
            money = sizing.dollars(trade, qty, self.cfg.commission_per_share)
            why = sizing.guard_reason(self.book, self.cfg.limits, n.symbol, trade["entry_date"], money["notional"],
                                      money["risk_dollars"])
            if why is not None:
                self._skip(run_date, n.bot, n.symbol, src, why)
                continue
            stop_source = ("derived" if plan.levels_source == "derived" else
                           "invalidation" if "invalidation" in plan.detail else "swing")
            record = {"origin": self.origin, "run_date": run_date.isoformat(), "source_file": src.path.name,
                      "report_as_of": src.as_of.isoformat(), "known_on": src.known_on.isoformat(),
                      "note": n.note, **trade, **money, "sizing": self.cfg.mode, "capped": capped,
                      "calm_scale": info.get("calm_scale"), "calm_scale_fallback": fallback,
                      "notional_limit": info.get("name_notional"), "stop_source": stop_source,
                      "entry_mode": self.cfg.entry_mode if n.bot == "side" else NEXT_DAY, **n.extra}
            self.trades.append(record)
            self.book.positions.append(record)

    def _planned(self, plan: Plan, run_date: date) -> tuple[float | None, float | None]:
        """(entry, risk per share) at ticket time: the limit for SIDE; the run date's close for UP / DOWN."""
        sign = 1 if plan.side == LONG else -1
        if plan.reference_entry is not None:
            return plan.reference_entry, plan.stop_offset or round(sign * (plan.reference_entry - plan.stop), 4)
        daily = self._daily(plan.symbol)
        upto = daily[daily["session"] <= run_date]
        if upto.empty:
            return None, None
        close = round(float(upto["close"].iloc[-1]), 4)
        return close, round(sign * (close - plan.stop), 4)

    def _indexes(self) -> pd.DataFrame:
        if self._index_daily is None:
            parts = []
            for symbol in confirmation.INDEXES:
                bars = self.cache.bars(symbol, DAILY)
                if not bars.empty:
                    parts.append(pd.DataFrame({"symbol": symbol, "date": pd.to_datetime(bars["session"]),
                                               "high": bars["high"], "low": bars["low"], "close": bars["close"]}))
            self._index_daily = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        return self._index_daily

    def _skip(self, run_date: date, bot: str, symbol: str, source: pit.Saved, reason: str) -> None:
        self.skipped.append({"origin": self.origin, "run_date": run_date.isoformat(), "bot": bot, "symbol": symbol,
                             "source_file": source.path.name, "report_as_of": source.as_of.isoformat(),
                             "reason": reason})

    def _daily(self, symbol: str):
        return self.cache.bars(symbol, DAILY)


def _handoff_side(entry: dict) -> str:
    side = str(entry.get("side") or "").lower()
    if side in (LONG, SHORT):
        return side
    return SHORT if str(entry.get("setup") or "").upper().startswith("SHORT") else LONG


def _reason_key(reason: str) -> str:
    """A short key for a funnel reason: the text before its first detail in brackets or after a colon."""
    text = reason.split(" (", 1)[0]
    if text.startswith("expired after") and "(last: " in reason:
        text = "expired: " + reason.split("(last: ", 1)[1].split(" (", 1)[0].split(";", 1)[0]
    return text[:70]


def _market(cache: BarCache, start: date, end: date, days: list[dict]) -> dict:
    """SPY and QQQ % change over the range, and how often each regime label came up."""
    moves = {}
    for symbol in confirmation.INDEXES:
        bars = cache.bars(symbol, DAILY)
        before = bars[bars["session"] < start]
        upto = bars[bars["session"] <= end]
        if not before.empty and not upto.empty:
            moves[symbol] = round((float(upto["close"].iloc[-1]) / float(before["close"].iloc[-1]) - 1) * 100, 2)
    labels = Counter(d["regime_label"] or "none" for d in days)
    return {"index_change_pct": moves, "regime_days": dict(labels)}


def _relabel_bars_end(trades: list[dict], cache: BarCache, timeframe: str) -> str | None:
    """Mark open_at_end trades whose bars stop more than BARS_END_SESSIONS before the data end as bars_end. Returns
    the data end (the latest bar among the symbols the run loaded)."""
    ends = [f["session"].iloc[-1] for (_, tf), f in cache._frames.items() if tf == timeframe and not f.empty]
    if not ends:
        return None
    data_end = max(ends)
    for t in trades:
        if t["exit_reason"] == OPEN_AT_END:
            gap = len(pd.bdate_range(t["exit_date"], data_end)) - 1
            if gap > BARS_END_SESSIONS:
                t["exit_reason"] = BARS_END
    return data_end.isoformat()


def _passes(home: Path, start: date, end: date, strategy: str, timeframe: str, held: int | None, cfg: Config,
            origins: tuple[str, ...], cache: BarCache) -> tuple[list[dict], list[_Pass]]:
    bots = BOTS if strategy == "all" else (strategy,)
    cover = coverage(home, start, end)
    by_status: dict[str, list[date]] = {}
    for row in cover:
        by_status.setdefault(row["status"], []).append(date.fromisoformat(row["date"]))
    passes = []
    if REPLAY in origins:
        passes.append(_Pass(REPLAY, home, by_status.get("saved", []), bots, cache, timeframe, held, cfg))
    if RECONSTRUCTED in origins:
        passes.append(_Pass(RECONSTRUCTED, pit.recon_home(home), by_status.get("reconstructed", []), bots, cache,
                            timeframe, held, cfg))
    for p in passes:
        p.run()
    return cover, passes


def candidate_symbols(home: str | Path, start: date, end: date, strategy: str = "all",
                      origins: tuple[str, ...] = ORIGINS) -> list[str]:
    """Every symbol a backtest of the range would simulate (regime matched, levels made), for fetching their bars."""
    _, passes = _passes(Path(home), start, end, strategy, DAILY, None, Config(collect_only=True, entry_mode=NEXT_DAY),
                        origins, BarCache(home))
    return sorted({s for p in passes for s in p.symbols})


def run_backtest(home: str | Path, start: date, end: date | None = None, strategy: str = "all",
                 timeframe: str = DAILY, max_sessions: int = 10, risk_pct: float = 1.0,
                 include_reconstructed: bool = True, hold_to_exit: bool = False, capital: float = 100_000.0, *,
                 origins: str | None = None, sizing_mode: str = sizing.LIVE, max_positions: int | None = None,
                 max_gross_pct: float | None = None, max_open_risk_pct: float | None = None,
                 slippage_bps: float = 0.0, commission_per_share: float = 0.0,
                 buying_power: float | None = None, entry_mode: str = NEXT_DAY, filters: dict | None = None,
                 expire_sessions: int | None = None, compare: bool = True) -> dict:
    """The backtest as a JSON-ready dict: params, rules, coverage, summary, curve, trades, skipped, days.
    ``origins`` is both, replay or reconstructed (default from ``include_reconstructed``). Limits left as None take
    the sizing mode's defaults (live: the guard's; fixed: none)."""
    end = end or start
    origins = origins or ("both" if include_reconstructed else REPLAY)
    defaults = sizing.LIVE_LIMITS if sizing_mode == sizing.LIVE else sizing.FIXED_LIMITS
    limits = sizing.Limits(
        max_positions=int(defaults["max_positions"] if max_positions is None else max_positions),
        max_gross_pct=float(defaults["max_gross_pct"] if max_gross_pct is None else max_gross_pct),
        max_open_risk_pct=float(defaults["max_open_risk_pct"] if max_open_risk_pct is None else max_open_risk_pct))
    _check(start, end, strategy, timeframe, max_sessions, risk_pct, capital)
    _check_more(sizing_mode, origins, limits, slippage_bps, commission_per_share, capital, buying_power)
    if entry_mode not in ENTRY_MODES:
        raise BacktestInputError(f"entry must be one of {', '.join(ENTRY_MODES)}")
    filters = {**tb.DEFAULT_FILTERS, **(filters or {})}
    if set(filters) != set(tb.FILTERS):
        raise BacktestInputError(f"filters must be on/off switches for {', '.join(tb.FILTERS)}")
    expire_sessions = tb.DEFAULT_EXPIRE if expire_sessions is None else int(expire_sessions)
    if not 1 <= expire_sessions <= 60:
        raise BacktestInputError("expire sessions must be 1 to 60")
    held = None if hold_to_exit else max_sessions  # None: no time exit, only the stop or the target
    home = Path(home)
    cfg = Config(mode=sizing_mode, risk_pct=risk_pct, capital=capital, buying_power=buying_power, limits=limits,
                 slippage_bps=slippage_bps, commission_per_share=commission_per_share, entry_mode=entry_mode,
                 filters=filters, expire_sessions=expire_sessions)
    cache = BarCache(home)
    chosen = ORIGIN_CHOICES[origins]
    cover, passes = _passes(home, start, end, strategy, timeframe, held, cfg, chosen, cache)
    trades = [t for p in passes for t in p.trades]
    data_end = _relabel_bars_end(trades, cache, timeframe)
    bots = BOTS if strategy == "all" else (strategy,)
    origin_names = tuple(p.origin for p in passes)
    summary, curve = summarize(trades, capital, start, bots, origin_names)
    funnel = None
    comparison = None
    if entry_mode == TRADEBOT and "side" in bots:
        funnel = {p.origin: p.funnel() for p in passes}
        for p in passes:
            done = [t for t in p.trades if t["bot"] == "side"]
            closed = [t for t in done if t["exit_reason"] not in NOT_CLOSED]
            side_skips = [k["reason"] for k in p.skipped if k["bot"] == "side"]
            funnel[p.origin].update(filled=len(done), closed=len(closed), won=sum(t["r"] > 0 for t in closed),
                                    no_levels=sum(r.startswith("no levels") for r in side_skips),
                                    limit_not_reached=sum("not reached" in r for r in side_skips),
                                    guard=sum(r.startswith(("already holding", "gross exposure", "open stop-risk"))
                                              or "positions already open" in r or "already fills" in r
                                              for r in side_skips))
        if compare:
            other = run_backtest(home, start, end, "side", timeframe, max_sessions, risk_pct, include_reconstructed,
                                 hold_to_exit, capital, origins=origins, sizing_mode=sizing_mode,
                                 max_positions=max_positions, max_gross_pct=max_gross_pct,
                                 max_open_risk_pct=max_open_risk_pct, slippage_bps=slippage_bps,
                                 commission_per_share=commission_per_share, buying_power=buying_power,
                                 entry_mode=NEXT_DAY, compare=False)
            side_only = run_backtest(home, start, end, "side", timeframe, max_sessions, risk_pct,
                                     include_reconstructed, hold_to_exit, capital, origins=origins,
                                     sizing_mode=sizing_mode, max_positions=max_positions,
                                     max_gross_pct=max_gross_pct, max_open_risk_pct=max_open_risk_pct,
                                     slippage_bps=slippage_bps, commission_per_share=commission_per_share,
                                     buying_power=buying_power, entry_mode=TRADEBOT, filters=filters,
                                     expire_sessions=expire_sessions, compare=False)
            pick = lambda rows: [r for r in rows if r["strategy"] == "side"]  # noqa: E731
            comparison = {TRADEBOT: pick(side_only["summary"]), NEXT_DAY: pick(other["summary"])}
    return {
        "params": {"start": start.isoformat(), "end": end.isoformat(), "strategy": strategy, "timeframe": timeframe,
                   "max_sessions": max_sessions, "hold_to_exit": hold_to_exit, "risk_pct": risk_pct,
                   "capital": capital, "buying_power": capital if buying_power is None else buying_power,
                   "max_report_age_days": MAX_REPORT_AGE_DAYS,
                   "include_reconstructed": RECONSTRUCTED in chosen, "origins": origins, "sizing": sizing_mode,
                   "max_positions": limits.max_positions, "max_gross_pct": limits.max_gross_pct,
                   "max_open_risk_pct": limits.max_open_risk_pct, "slippage_bps": slippage_bps,
                   "commission_per_share": commission_per_share, "data_end": data_end,
                   "entry_mode": entry_mode, "filters": filters, "expire_sessions": expire_sessions},
        "rules": RULES,
        "caveats": {o: CAVEATS[o] for o in chosen},
        "assumptions": ASSUMPTIONS,
        "calm_scale_fallback_dates": sorted({d for p in passes for d in p.calm_fallback}),
        "coverage": cover,
        "summary": summary,
        "breakdown": breakdown(trades, capital, origin_names),
        "funnel": funnel,
        "comparison": comparison,
        "market": _market(cache, start, end, [d for p in passes for d in p.days]),
        "curve": curve,
        "trades": trades,
        "skipped": [s for p in passes for s in p.skipped],
        "days": [d for p in passes for d in p.days],
        "warnings": [w for p in passes for w in p.lib.warnings],
    }

