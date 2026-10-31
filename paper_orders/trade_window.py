"""The operator's trading window: which weekdays and New York session times paper orders may be sent in.

The window lives in the "trading" section of ~/.tradingagents/settings.json (or the file named by
TRADINGAGENTS_SETTINGS_PATH), which the local UI's Settings page writes. ``paper_orders.sender`` refuses a
--send outside it. With no settings file, or no "trading" section, there is no window and nothing is gated,
so the sender behaves as it did before the window existed.

Standard library and pytz only: importing this loads no Alpaca module, so the UI and the scheduler can read
the window without loading Alpaca's trading client. Market holidays and early closes come from
``paper_orders.market_calendar``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

import pytz

from paper_orders import market_calendar

SETTINGS_PATH_ENV = "TRADINGAGENTS_SETTINGS_PATH"
MARKET_TZ = pytz.timezone("America/New_York")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class SettingsError(ValueError):
    """The settings file or one of its sections is malformed."""


def settings_path() -> Path:
    override = os.environ.get(SETTINGS_PATH_ENV)
    return Path(override) if override else Path.home() / ".tradingagents" / "settings.json"


def load_settings(path: Path | None = None) -> dict:
    """The whole settings file, or {} when there is none. A file that is there but unreadable is an error."""
    path = path or settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SettingsError(f"cannot read settings file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"settings file {path} is not a JSON object")
    return data


def parse_hhmm(value: str, what: str = "time") -> time:
    match = _HHMM.match(value or "")
    if not match:
        raise SettingsError(f"{what} {value!r} is not HH:MM (24-hour)")
    return time(int(match[1]), int(match[2]))


def parse_days(values) -> tuple[str, ...]:
    days = tuple(dict.fromkeys(str(d).lower() for d in values or ()))
    unknown = [d for d in days if d not in WEEKDAYS]
    if unknown:
        raise SettingsError(f"unknown day(s): {', '.join(unknown)}")
    return days


@dataclass(frozen=True)
class TradeWindow:
    days: tuple[str, ...]
    sessions: tuple[tuple[time, time], ...]

    @classmethod
    def from_dict(cls, data: dict) -> TradeWindow:
        days = parse_days(data.get("days"))
        sessions = []
        for s in data.get("sessions") or []:
            start = parse_hhmm(s.get("start"), "session start")
            end = parse_hhmm(s.get("end"), "session end")
            if not start < end:
                raise SettingsError(f"session {s.get('start')}-{s.get('end')} does not end after it starts")
            sessions.append((start, end))
        return cls(days, tuple(sorted(sessions)))

    def closed_reason(self, when: datetime) -> str | None:
        """Why `when` (any time zone) is outside the window, or None when orders may be sent.

        A market holiday is closed even on an allowed weekday, and on an early-close day (1:00 PM) every
        session ends at the close.
        """
        local = when.astimezone(MARKET_TZ)
        day = local.date()
        if WEEKDAYS[local.weekday()] not in self.days:
            return f"{local:%A} is not a trading day in Settings"
        if not market_calendar.is_trading_day(day):
            return f"{day} is a market holiday"
        now = local.time().replace(second=0, microsecond=0)
        close = market_calendar.early_close(day)
        if any(start <= now < (min(end, close) if close else end) for start, end in self.sessions):
            return None
        if close and any(start <= now < end for start, end in self.sessions):
            return f"{day} is an early-close day; the market closes at {close:%H:%M}"
        return f"{now:%H:%M} New York is outside the sessions"

    def allows(self, when: datetime) -> bool:
        """True on an allowed trading day, inside a session, before any early close."""
        return self.closed_reason(when) is None

    def describe(self) -> str:
        days = ", ".join(d.capitalize() for d in self.days) or "no days"
        sessions = ", ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in self.sessions) or "no sessions"
        return f"{days}; {sessions} New York time; market holidays skipped, early closes at 13:00"


def load_window(path: Path | None = None) -> TradeWindow | None:
    """The saved trading window, or None when none is saved (nothing is gated)."""
    trading = load_settings(path).get("trading")
    if trading is None:
        return None
    if not isinstance(trading, dict):
        raise SettingsError("settings 'trading' section is not an object")
    return TradeWindow.from_dict(trading)
