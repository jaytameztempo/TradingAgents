"""Settings the UI saves: the automation schedule and the trading window, in one JSON file.

The file is ~/.tradingagents/settings.json (paths.settings_path()). Its "trading" section is the window
``paper_orders.sender`` enforces (see paper_orders/trade_window.py), so it is validated with the same parser
the sender uses. Writes go through a temp file and a rename, so a reader never sees half a file.
"""

from __future__ import annotations

import json
import os
from typing import Any

from paper_orders.trade_window import SettingsError, TradeWindow, load_settings, parse_days, parse_hhmm
from ui.server import paths

AUTOMATION_RUNS = {
    "scans": "All four scans (trend + sideways)",
    "daily": "Daily SIDE pipeline (run_daily: regime → … → paper tickets; sends nothing)",
}

DEFAULT_AUTOMATION: dict[str, Any] = {
    "enabled": False,
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "times": ["16:30"],
    "run": "daily",
}

DEFAULT_TRADING: dict[str, Any] = {
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "sessions": [{"start": "09:35", "end": "15:55"}],
    "auto_send": {"enabled": False, "times": ["09:40"]},
}


def read_all() -> dict:
    return load_settings(paths.settings_path())


def automation() -> dict:
    return {**DEFAULT_AUTOMATION, **(read_all().get("automation") or {})}


def trading(saved_only: bool = False) -> dict | None:
    saved = read_all().get("trading")
    if saved_only:
        return saved
    return {**DEFAULT_TRADING, **(saved or {})}


def _times(values, what: str) -> list[str]:
    times = sorted(dict.fromkeys(values or []))
    for t in times:
        parse_hhmm(t, what)
    return times


def validate_automation(data: dict) -> dict:
    run = data.get("run", "daily")
    if run not in AUTOMATION_RUNS:
        raise SettingsError(f"run must be one of {', '.join(AUTOMATION_RUNS)}")
    clean = {
        "enabled": bool(data.get("enabled")),
        "days": list(parse_days(data.get("days"))),
        "times": _times(data.get("times"), "scan time"),
        "run": run,
    }
    if clean["enabled"] and not (clean["days"] and clean["times"]):
        raise SettingsError("pick at least one day and one time to switch recurring scans on")
    return clean


def validate_trading(data: dict) -> dict:
    window = TradeWindow.from_dict(data)  # same rules the sender applies
    for (a_start, a_end), (b_start, _) in zip(window.sessions, window.sessions[1:], strict=False):
        if b_start < a_end:
            raise SettingsError(f"sessions {a_start:%H:%M}-{a_end:%H:%M} and {b_start:%H:%M} overlap")
    auto = data.get("auto_send") or {}
    send_times = _times(auto.get("times"), "auto-send time")
    for t in send_times:
        at = parse_hhmm(t)
        if not any(start <= at < end for start, end in window.sessions):
            raise SettingsError(f"auto-send time {t} is not inside a session")
    enabled = bool(auto.get("enabled"))
    if enabled and not (window.days and window.sessions and send_times):
        raise SettingsError("auto-send needs at least one trading day, one session and one send time")
    return {
        "days": list(window.days),
        "sessions": [{"start": f"{a:%H:%M}", "end": f"{b:%H:%M}"} for a, b in window.sessions],
        "auto_send": {"enabled": enabled, "times": send_times},
    }


def save_section(name: str, value: dict) -> dict:
    data = read_all()
    data[name] = value
    path = paths.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return value
