"""In-app scheduler: recurring scans, and paper auto-send inside the trading window.

It runs on a background thread while the UI server runs (``python -m ui.server``); nothing runs while the
app is closed, and a time missed while it was closed is not made up later. Every 20 seconds it checks the
settings saved on the Settings page, in New York time:

- recurring scans: on each chosen day, at each chosen time, it starts either the four scans or run_daily
  (which writes paper tickets but sends nothing).
- auto-send: only when switched on, on each trading day, at each send time that is inside a session, it
  starts ``python -m paper_orders.sender --as-of <date> --send`` for the newest ticket file dated before
  today (at most MAX_TICKET_AGE_DAYS old). The sender re-checks the window, refuses non-paper keys, and
  never sends a client order ID twice, so a repeat run only places exits for entries that filled since.

Each slot fires at most once a day: what fired is kept in scheduler_state.json beside the settings.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from paper_orders.trade_window import MARKET_TZ, WEEKDAYS, SettingsError, TradeWindow, parse_hhmm
from ui.server import jobs, paths, settings
from ui.server.routes import common

TICK_SECONDS = 20
GRACE = timedelta(minutes=5)  # a slot still fires if the app noticed it up to 5 minutes late
SCAN_CLOSE_CUTOFF = parse_hhmm("16:15")  # before this, scans are dated the previous weekday
MAX_TICKET_AGE_DAYS = 5
MAX_EVENTS = 50
_TICKET_FILE = re.compile(r"^paper_tickets_(\d{4}-\d{2}-\d{2})_\d{8}T\d{6}Z\.json$")


@dataclass
class Event:
    at: str
    kind: str  # scan or send
    slot: str
    outcome: str  # started, skipped, error
    detail: str
    job_id: str | None = None


def previous_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def scan_as_of(local: datetime) -> date:
    """After the close (16:15 New York) on a weekday, today's bars are complete; otherwise the last weekday's."""
    if local.weekday() < 5 and local.time() >= SCAN_CLOSE_CUTOFF:
        return local.date()
    return previous_weekday(local.date())


def newest_ticket_date(today: date) -> date | None:
    """The newest ticket file's date before today, if it is at most MAX_TICKET_AGE_DAYS old."""
    folder = paths.tickets_dir()
    if not folder.is_dir():
        return None
    dates = {date.fromisoformat(m[1]) for p in folder.iterdir() if (m := _TICKET_FILE.match(p.name))}
    candidates = [d for d in dates if d < today and (today - d).days <= MAX_TICKET_AGE_DAYS]
    return max(candidates) if candidates else None


class Scheduler:
    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 start: Callable[..., jobs.Job] = jobs.runner.start) -> None:
        self._clock = clock
        self._start = start
        self._fired: set[str] = set()
        self._events: list[Event] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
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

    def tick(self) -> None:
        now = self._clock()
        local = now.astimezone(MARKET_TZ)
        day = WEEKDAYS[local.weekday()]
        changed = False

        auto = settings.automation()
        if auto["enabled"] and day in auto["days"]:
            for t in auto["times"]:
                key = f"scan|{local.date()}|{t}"
                if key not in self._fired and self._due(local, t):
                    self._fired.add(key)
                    self._run_scans(local, t, auto["run"])
                    changed = True

        trading = settings.trading(saved_only=True)
        auto_send = (trading or {}).get("auto_send") or {}
        if trading and auto_send.get("enabled") and day in trading.get("days", []):
            window = TradeWindow.from_dict(trading)
            for t in auto_send.get("times", []):
                key = f"send|{local.date()}|{t}"
                if key not in self._fired and self._due(local, t):
                    self._fired.add(key)
                    if window.allows(now):
                        self._run_sender(local, t)
                    else:
                        self._record("send", t, "skipped", "outside the trading window")
                    changed = True

        if changed:
            self._save_state()

    def _run_scans(self, local: datetime, slot: str, run: str) -> None:
        as_of = scan_as_of(local).isoformat()
        if run == "daily":
            steps = [jobs.Step("run_daily", ["--as-of", as_of])]
        else:
            steps = [*common.scan_steps(as_of, []), *common.channel_scan_steps(as_of)]
        try:
            job = self._start(f"scheduled:{run}", steps, "pipeline")
        except jobs.JobConflict as exc:
            self._record("scan", slot, "skipped", f"{exc}")
            return
        self._record("scan", slot, "started", f"{settings.AUTOMATION_RUNS[run]} for {as_of}", job.id)

    def _run_sender(self, local: datetime, slot: str) -> None:
        as_of = newest_ticket_date(local.date())
        if as_of is None:
            self._record("send", slot, "skipped",
                         f"no paper ticket file dated in the {MAX_TICKET_AGE_DAYS} days before today")
            return
        step = jobs.Step(jobs.SENDER, ["--as-of", as_of.isoformat(), "--send"])
        try:
            job = self._start("scheduled:paper-send", [step], "sender")
        except jobs.JobConflict as exc:
            self._record("send", slot, "skipped", f"{exc}")
            return
        self._record("send", slot, "started", f"paper tickets for {as_of}", job.id)

    # --- status for the Settings page -----------------------------------------

    def _next(self, days, times, after: datetime) -> str | None:
        local = after.astimezone(MARKET_TZ)
        for offset in range(8):
            d = local.date() + timedelta(days=offset)
            if WEEKDAYS[d.weekday()] not in days:
                continue
            for t in sorted(times):
                at = parse_hhmm(t)
                slot = MARKET_TZ.localize(datetime(d.year, d.month, d.day, at.hour, at.minute))
                if slot > local:
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
        return {
            "running": self.running,
            "now": now.astimezone(MARKET_TZ).isoformat(timespec="seconds"),
            "next_scan": self._next(auto["days"], auto["times"], now) if auto["enabled"] else None,
            "next_send": (self._next(trading.get("days", []), auto_send.get("times", []), now)
                          if trading and auto_send.get("enabled") else None),
            "auto_send_enabled": bool(trading and auto_send.get("enabled")),
            "window_saved": trading is not None,
            "events": [e.__dict__ for e in reversed(self._events)],
        }


scheduler = Scheduler()
