"""Backtest a date range: what would the system have traded, and how would it have done?

Each weekday in the range has one source:
- **replay**: the date has a saved route, so its trades come from the files the pipeline saved;
- **reconstructed**: the date was rebuilt by ``run_backtest_reconstruct`` (see ``reconstruct.py``), so its trades come
  from the rebuilt files under ``backtest/reconstructed``;
- otherwise nothing (the page offers to rebuild it).

For an as-of date A, each kind of file used is the newest one with as-of A written within MAX_REPORT_AGE_DAYS of A
(same-week re-runs count, much later re-runs are ignored). A signal from a file is acted on the day that file was
written (its New York date): the regime label is the newest route visible that day, and entry is the open of the next
session. The trades:
1. SIDEBot: the PLAN names of the side handoff (ranked first); with no handoff, the pivot-confirmed channel report.
2. UPBot / DOWNBot: the route's ``up_passed`` / ``down_passed`` (levels from its basket), and the trend handoff.
3. The live regime rule: UPBot only on UP, DOWNBot only on DOWN, SIDEBot only on SIDE.
4. One position a symbol a bot, within each origin. The two origins are never merged.
Levels per ``levels.py``; the trade per ``simulate.py``.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from extensions.backtest import pit
from extensions.backtest.bars import DAILY, TIMEFRAMES, BarCache
from extensions.backtest.levels import LONG, RULES, SHORT, LevelsError, Plan, side_plan_from_channel, \
    side_plan_from_handoff, trend_plan_for
from extensions.backtest.simulate import BOTS, Skip, simulate, summarize

STRATEGIES = ("all", *BOTS)
REPLAY, RECONSTRUCTED = "replay", "reconstructed"
ORIGINS = (REPLAY, RECONSTRUCTED)
BOT_REGIME = {"up": "UP", "down": "DOWN", "side": "SIDE"}
BOT_SIDE = {"up": LONG, "down": SHORT}
MAX_REPORT_AGE_DAYS = 10  # a file counts for its as-of only if written within this many days; routes as labels too
MAX_RANGE_DAYS = 366
MAX_SESSIONS = 60

CAVEATS = {
    REPLAY: "Replayed dates use the files the pipeline saved, under the rules of the code that ran then, entering the "
            "session after each file was actually written.",
    RECONSTRUCTED: "Rebuilt dates run today's pipeline code on bars up to the as-of date, entering the next session's "
                   "open. Not point in time: the universe and borrow flags come from today's asset snapshot "
                   "(survivorship), the spread gate is not applied, fundamentals are today's cached SEC files (facts "
                   "filed by the as-of date; missing ones are downloaded only when SEC download is on), and an "
                   "unmapped sector makes the side handoff unranked.",
}


class BacktestInputError(ValueError):
    """The request cannot run (bad dates, strategy, timeframe or numbers)."""


def _check(start: date, end: date, strategy: str, timeframe: str, max_sessions: int | None,
           risk_pct: float) -> None:
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
    """One file a signal comes from, and the bot / side it feeds."""

    def __init__(self, bot: str, side: str, saved: pit.Saved, kind: str):
        self.bot, self.side, self.saved, self.kind = bot, side, saved, kind


class _Pass:
    """One origin's replay over one data home."""

    def __init__(self, origin: str, home: Path, dates: list[date], bots: tuple[str, ...], cache: BarCache,
                 timeframe: str, max_sessions: int | None):
        self.origin, self.dates, self.bots = origin, dates, bots
        self.lib = pit.Library(home)
        self.cache, self.timeframe, self.max_sessions = cache, timeframe, max_sessions
        self.held_until: dict[tuple[str, str], str] = {}
        self.trades: list[dict] = []
        self.skipped: list[dict] = []
        self.days: list[dict] = []

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
        # Act in the order the files were written, so "one position a symbol" runs forward in time.
        sources.sort(key=lambda s: (s.saved.known_on, s.saved.created_at, s.bot, s.side))
        for source in sources:
            self._act(source)

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
            for side, kind in ((LONG, "long_channel"), (SHORT, "short_channel")):
                if handoff is not None:
                    out.append(_Source("side", side, handoff, "handoff"))
                else:
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

    # --- acting on one file ----------------------------------------------------------------

    def _act(self, source: _Source) -> None:
        run_date = source.saved.known_on
        route = self.lib.newest("route", run_date)
        if route is not None and (run_date - route.as_of).days > MAX_REPORT_AGE_DAYS:
            route = None
        label = str(route.data.get("regime_label") or "") if route else None
        saved, bot, side, data = source.saved, source.bot, source.side, source.saved.data

        if source.kind == "handoff":
            report = self.lib.referenced("scanbot", data.get(f"{side}_channel_report"), run_date)
            for symbol, entry in (data.get("names") or {}).items():
                if entry.get("status") == "PLAN" and _handoff_side(entry) == side:
                    self._candidate(run_date, bot, symbol, label, saved,
                                    lambda s=symbol, e=entry: side_plan_from_handoff(s, side, e,
                                                                                    report and report.data))
        elif source.kind == "channel":
            for symbol in data.get("members") or []:
                self._candidate(run_date, bot, symbol, label, saved,
                                lambda s=symbol: side_plan_from_channel(s, side, data),
                                note="no handoff; channel report levels")
        elif source.kind == "route":
            basket = self.lib.referenced("baskets", data.get("basket_file" if bot == "up" else "down_basket_file"),
                                         run_date)
            members = {m.get("ticker"): m.get("metrics") for m in (basket.data.get("members") or [])} if basket else {}
            as_of = basket.as_of if basket else saved.as_of
            for symbol in data.get("up_passed" if bot == "up" else "down_passed") or []:
                self._candidate(run_date, bot, symbol, label, saved,
                                lambda s=symbol: trend_plan_for(bot, s, side, members.get(s), self._daily(s), as_of))
        else:  # trend handoff (4-hour confirmed)
            for symbol, entry in (data.get("names") or {}).items():
                if entry.get("side") != BOT_REGIME[bot]:
                    continue
                if entry.get("delisted"):
                    self._skip(run_date, bot, symbol, saved, f"delisted after {entry['delisted'].get('after')}")
                    continue
                self._candidate(run_date, bot, symbol, label, saved,
                                lambda s=symbol: trend_plan_for(bot, s, side, None, self._daily(s), saved.as_of),
                                note="4-hour confirmed")

    def _candidate(self, run_date: date, bot: str, symbol: str, label: str | None, source: pit.Saved, make_plan,
                   note: str = "") -> None:
        if label is None:
            return self._skip(run_date, bot, symbol, source, "no route visible on this date: no regime label")
        if label != BOT_REGIME[bot]:
            return self._skip(run_date, bot, symbol, source, f"regime {label} does not match {BOT_REGIME[bot]}")
        try:
            plan: Plan = make_plan()
        except LevelsError as exc:
            return self._skip(run_date, bot, symbol, source, f"no levels: {exc}")
        bars = self.cache.bars(symbol, self.timeframe)
        try:
            trade = simulate(plan, bars, run_date, self.max_sessions)
        except Skip as exc:
            word = "4-hour " if self.timeframe != DAILY else ""
            return self._skip(run_date, bot, symbol, source, str(exc).replace("cached bars", f"cached {word}bars"))
        held = self.held_until.get((bot, symbol))
        if held is not None and trade["entry_date"] <= held:
            return self._skip(run_date, bot, symbol, source, f"already holding {symbol} until {held}")
        self.held_until[(bot, symbol)] = trade["exit_date"]
        self.trades.append({"origin": self.origin, "run_date": run_date.isoformat(), "source_file": source.path.name,
                            "report_as_of": source.as_of.isoformat(), "known_on": source.known_on.isoformat(),
                            "note": note, **trade})

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


def run_backtest(home: str | Path, start: date, end: date | None = None, strategy: str = "all",
                 timeframe: str = DAILY, max_sessions: int = 10, risk_pct: float = 1.0,
                 include_reconstructed: bool = True, hold_to_exit: bool = False) -> dict:
    """The backtest as a JSON-ready dict: params, rules, coverage, summary, curve, trades, skipped, days."""
    end = end or start
    _check(start, end, strategy, timeframe, max_sessions, risk_pct)
    held = None if hold_to_exit else max_sessions  # None: no time exit, only the stop or the target
    home = Path(home)
    bots = BOTS if strategy == "all" else (strategy,)
    cover = coverage(home, start, end)
    by_status: dict[str, list[date]] = {}
    for row in cover:
        by_status.setdefault(row["status"], []).append(date.fromisoformat(row["date"]))

    cache = BarCache(home)
    passes = [_Pass(REPLAY, home, by_status.get("saved", []), bots, cache, timeframe, held)]
    if include_reconstructed:
        passes.append(_Pass(RECONSTRUCTED, pit.recon_home(home), by_status.get("reconstructed", []), bots, cache,
                            timeframe, held))
    for p in passes:
        p.run()
    trades = [t for p in passes for t in p.trades]
    origins = tuple(p.origin for p in passes)
    summary, curve = summarize(trades, risk_pct, start, bots, origins)
    return {
        "params": {"start": start.isoformat(), "end": end.isoformat(), "strategy": strategy, "timeframe": timeframe,
                   "max_sessions": max_sessions, "hold_to_exit": hold_to_exit, "risk_pct": risk_pct, "max_report_age_days": MAX_REPORT_AGE_DAYS,
                   "include_reconstructed": include_reconstructed},
        "rules": RULES,
        "caveats": CAVEATS,
        "coverage": cover,
        "summary": summary,
        "curve": curve,
        "trades": trades,
        "skipped": [s for p in passes for s in p.skipped],
        "days": [d for p in passes for d in p.days],
        "warnings": [w for p in passes for w in p.lib.warnings],
    }
