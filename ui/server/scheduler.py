"""In-app scheduler: recurring scans, the Tradebot's checks, paper auto-send, and reconcile.

It runs on a background thread while the UI server runs (``python -m ui.server``); nothing runs while the
app is closed. Every 20 seconds it checks the settings saved on the Settings page, in New York time:

- recurring scans: on each chosen day (weekends included), at each chosen time, it starts either the three
  category scans (upward, downward, sideways) at the saved category, or run_daily at that category (which fills
  the Tradebot queue and runs its eod trigger check, but sends nothing), dated the last open session whose bars
  are complete: Friday for a weekend run, the session before a market holiday (see scan_as_of).
- Tradebot intraday checks (tradebot mode semi or auto), at each intraday time inside the trading window:
  ``run_tradebot --stage intraday``; in auto mode also ``--ticket``, then ``paper_orders.sender --tradebot
  --send``, then ``paper_orders.reconcile``.
- auto-send (tradebot mode auto AND the paper auto-send switch on), at each send time inside the window:
  ``run_tradebot --ticket-only``, then ``paper_orders.sender --tradebot --send``, then reconcile. The sender
  re-checks the window and the risk guard, refuses non-paper keys, and never sends a client order ID twice, so a
  repeat run only places exits for entries that filled since. The old automatic send of the daily paper ticket
  file is retired: those files are sent by hand (``sender --as-of D --send``).
- reconcile at 16:05 on trading days (tradebot mode semi or auto), read-only.

In those sending chains the sender and reconcile steps run even when the step before them failed, so exits are
placed whatever happened to the tickets. A missed intraday or send slot is made up once when the app comes back,
if it is still the same trading day and the window is open: only the latest missed one runs, the others are
recorded as skipped. Missed scan and reconcile slots are not made up.

Each slot fires at most once a day: what fired is kept in scheduler_state.json beside the settings.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from paper_orders import market_calendar
from paper_orders.trade_window import MARKET_TZ, WEEKDAYS, SettingsError, TradeWindow, parse_hhmm
from ui.server import jobs, paths, settings
from ui.server.routes import common

TICK_SECONDS = 20
GRACE = timedelta(minutes=5)  # a slot still fires if the app noticed it up to 5 minutes late
SCAN_CLOSE_CUTOFF = parse_hhmm("16:15")  # before this, scans are dated the previous weekday
MAX_EVENTS = 50
RECONCILE_AT = "16:05"


@dataclass
class Event:
    at: str
    kind: str  # scan or send
    slot: str
    outcome: str  # started, skipped, error
    detail: str
    job_id: str | None = None


def scan_as_of(local: datetime) -> date:
    """The last open session whose daily bars are complete.

    On a trading day after the close (16:15 New York), that is today. Before the close, or on a Saturday,
    Sunday or market holiday, it is the last trading day before today: Friday for a weekend run, or
    Thursday when that Friday was a holiday such as Good Friday.
    """
    if market_calendar.is_trading_day(local.date()) and local.time() >= SCAN_CLOSE_CUTOFF:
        return local.date()
    return market_calendar.previous_trading_day(local.date())


class Scheduler:
    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 start: Callable[..., jobs.Job] = jobs.runner.start) -> None:
        self._clock = clock
        self._start = start
        self._fired: set[str] = set()
        self._events: list[Event] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick: datetime | None = None
        self._load_state()

    # --- state --------------------------------------------------------------

    def _state_path(self):
        return paths.settings_path().with_name("scheduler_state.json")

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
            self._fired = set(data.get("fired", []))
            self._events = [Event(**e) for e in data.get("events", [])][-MAX_EVENTS:]
        except (OSError, ValueError, TypeError):
            pass

    def _save_state(self) -> None:
        # Keep only the last week of fired slots; older ones can never match again.
        cutoff = (self._clock().astimezone(MARKET_TZ).date() - timedelta(days=7)).isoformat()
        self._fired = {k for k in self._fired if k.split("|")[1] >= cutoff}
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fired": sorted(self._fired),
                                       "events": [e.__dict__ for e in self._events[-MAX_EVENTS:]]}, indent=1),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _record(self, kind: str, slot: str, outcome: str, detail: str, job_id: str | None = None) -> None:
        self._events.append(Event(self._clock().isoformat(timespec="seconds"), kind, slot, outcome, detail, job_id))
        self._events = self._events[-MAX_EVENTS:]

    # --- the loop -----------------------------------------------------------

    def start_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bats-scheduler", daemon=True)
        self._thread.start()

    def stop_thread(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # a bad settings file must not kill the thread
                self._record("scheduler", "-", "error", str(exc))
            self._stop.wait(TICK_SECONDS)

    def _due(self, local: datetime, hhmm: str) -> bool:
        at = parse_hhmm(hhmm)
        slot = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        return timedelta(0) <= local - slot < GRACE

    def _passed(self, local: datetime, hhmm: str) -> bool:
        at = parse_hhmm(hhmm)
        return local >= local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)

    def _catch_up(self, kind: str, local: datetime, times: list[str]) -> str | None:
        """The slot to run now among today's unfired ones that have passed: the latest. Earlier missed ones are
        marked fired and recorded as skipped. None when nothing is due."""
        missed = [t for t in sorted(times) if f"{kind}|{local.date()}|{t}" not in self._fired and self._passed(local, t)]
        if not missed:
            return None
        for t in missed[:-1]:
            self._fired.add(f"{kind}|{local.date()}|{t}")
            self._record(kind, t, "skipped", "missed while the app was closed; a later slot runs instead")
        self._fired.add(f"{kind}|{local.date()}|{missed[-1]}")
        return missed[-1]

    def tick(self) -> None:
        now = self._clock()
        self.last_tick = now
        local = now.astimezone(MARKET_TZ)
        day = WEEKDAYS[local.weekday()]
        changed = False

        auto = settings.automation()
        if auto["enabled"] and day in auto["days"]:
            for t in auto["times"]:
                key = f"scan|{local.date()}|{t}"
                if key not in self._fired and self._due(local, t):
                    self._fired.add(key)
                    self._run_scans(local, t, auto["run"], auto["category"])
                    changed = True

        try:
            tb = settings.tradebot()
        except SettingsError as exc:
            key = f"settings|{local.date()}"
            if key not in self._fired:
                self._fired.add(key)
                self._record("tradebot", "-", "error", f"tradebot settings: {exc}; the Tradebot is not scheduled")
                changed = True
            tb = {"mode": "manual", "intraday_times": []}
        trading = settings.trading(saved_only=True)
        window = TradeWindow.from_dict(trading) if trading else None
        trading_day = market_calendar.is_trading_day(local.date())
        mode = tb["mode"]

        if mode in ("semi", "auto") and trading_day:
            if window is None:
                key = f"nowindow|{local.date()}"
                if key not in self._fired and any(self._passed(local, t) for t in tb["intraday_times"]):
                    self._fired.add(key)
                    self._record("intraday", "-", "skipped", "no trading window is saved; the Tradebot's intraday "
                                                             "checks need one")
                    changed = True
            elif window.closed_reason(now) is None:
                t = self._catch_up("intraday", local, tb["intraday_times"])
                if t:
                    self._run_intraday(t, mode)
                    changed = True
            if self._due(local, RECONCILE_AT) and f"reconcile|{local.date()}|{RECONCILE_AT}" not in self._fired:
                self._fired.add(f"reconcile|{local.date()}|{RECONCILE_AT}")
                self._run_reconcile(RECONCILE_AT)
                changed = True

        auto_send = (trading or {}).get("auto_send") or {}
        if trading and auto_send.get("enabled") and day in trading.get("days", []):
            times = auto_send.get("times", [])
            closed = window.closed_reason(now)
            if closed is None:
                t = self._catch_up("send", local, times)
                if t:
                    if mode == "auto":
                        self._run_auto_send(t)
                    else:
                        self._record("send", t, "skipped", f"tradebot mode is {mode}; automatic sends need auto mode "
                                                           "and paper auto-send both on")
                    changed = True
            else:
                for t in times:
                    key = f"send|{local.date()}|{t}"
                    if key not in self._fired and self._due(local, t):
                        self._fired.add(key)
                        self._record("send", t, "skipped", f"outside the trading window: {closed}")
                        changed = True

        if changed:
            self._save_state()

    def _start_job(self, kind: str, slot: str, name: str, steps: list[jobs.Step], group: str, detail: str) -> None:
        try:
            job = self._start(name, steps, group)
        except jobs.JobConflict as exc:
            self._record(kind, slot, "skipped", f"{exc}")
            return
        self._record(kind, slot, "started", detail, job.id)

    def _run_scans(self, local: datetime, slot: str, run: str, category: int = 1) -> None:
        as_of = scan_as_of(local).isoformat()
        if run == "daily":
            steps = [jobs.Step("run_daily", ["--as-of", as_of, "--category", str(category)])]
            what = settings.AUTOMATION_RUNS[run]
        else:
            steps = [*common.input_steps(as_of), *common.category_scan_steps(as_of, common.CATEGORY_SCANS, category)]
            what = f"{settings.AUTOMATION_RUNS[run]}, category {category}"
        self._start_job("scan", slot, f"scheduled:{run}", steps, "pipeline", f"{what} for {as_of}")

    def _run_intraday(self, slot: str, mode: str) -> None:
        if mode == "auto":
            steps = [jobs.Step("run_tradebot", ["--stage", "intraday", "--ticket"]),
                     jobs.Step(jobs.SENDER, ["--tradebot", "--send"], always=True),
                     jobs.Step(jobs.RECONCILE, [], always=True)]
            self._start_job("intraday", slot, "scheduled:tradebot-intraday-auto", steps, "sender",
                            "intraday check, tickets, send and reconcile")
        else:
            self._start_job("intraday", slot, "scheduled:tradebot-intraday", [
                jobs.Step("run_tradebot", ["--stage", "intraday"])], "pipeline", "intraday trigger check")

    def _run_auto_send(self, slot: str) -> None:
        steps = [jobs.Step("run_tradebot", ["--ticket-only"]),
                 jobs.Step(jobs.SENDER, ["--tradebot", "--send"], always=True),
                 jobs.Step(jobs.RECONCILE, [], always=True)]
        self._start_job("send", slot, "scheduled:tradebot-send", steps, "sender",
                        "tickets for triggered items, the Tradebot send, and reconcile")

    def _run_reconcile(self, slot: str) -> None:
        self._start_job("reconcile", slot, "scheduled:reconcile", [jobs.Step(jobs.RECONCILE, [])], "reconcile",
                        "reconcile the paper account (read-only)")

    # --- status for the Settings page -----------------------------------------

    def _next(self, days, times, after: datetime, window: TradeWindow | None = None) -> str | None:
        """The next slot after `after`; with a window, only slots it allows (no holiday, no early-close tail)."""
        local = after.astimezone(MARKET_TZ)
        for offset in range(15):
            d = local.date() + timedelta(days=offset)
            if WEEKDAYS[d.weekday()] not in days:
                continue
            for t in sorted(times):
                at = parse_hhmm(t)
                slot = MARKET_TZ.localize(datetime(d.year, d.month, d.day, at.hour, at.minute))
                if slot > local and (window is None or window.allows(slot)):
                    return slot.isoformat()
        return None

    def status(self) -> dict:
        now = self._clock()
        auto = settings.automation()
        try:
            trading = settings.trading(saved_only=True)
        except SettingsError:
            trading = None
        auto_send = (trading or {}).get("auto_send") or {}
        next_scan = self._next(auto["days"], auto["times"], now) if auto["enabled"] else None
        try:
            tb = settings.tradebot()
        except SettingsError:
            tb = {"mode": "manual", "intraday_times": []}
        next_intraday = (self._next(list(WEEKDAYS), tb["intraday_times"], now, TradeWindow.from_dict(trading))
                         if trading and tb["mode"] in ("semi", "auto") else None)
        return {
            "running": self.running,
            "last_tick": self.last_tick.isoformat(timespec="seconds") if self.last_tick else None,
            "tradebot_mode": tb["mode"],
            "next_intraday": next_intraday,
            "auto_send_sends": bool(trading and auto_send.get("enabled") and tb["mode"] == "auto"),
            "now": now.astimezone(MARKET_TZ).isoformat(timespec="seconds"),
            "next_scan": next_scan,
            "next_scan_as_of": scan_as_of(datetime.fromisoformat(next_scan)).isoformat() if next_scan else None,
            "next_send": (self._next(trading.get("days", []), auto_send.get("times", []), now,
                                     TradeWindow.from_dict(trading))
                          if trading and auto_send.get("enabled") else None),
            "auto_send_enabled": bool(trading and auto_send.get("enabled")),
            "window_saved": trading is not None,
            "events": [e.__dict__ for e in reversed(self._events)],
        }


scheduler = Scheduler()
