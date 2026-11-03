"""The Tradebot's settings: the "tradebot" section of settings.json, over these defaults.

The file is the one the Settings page writes (paper_orders.trade_window.settings_path: TRADINGAGENTS_SETTINGS_PATH,
else ~/.tradingagents/settings.json). A setting that is missing takes its default; one that is present but not
valid is an error, so a typo never quietly loosens a check.
"""

from __future__ import annotations

from typing import Any

from paper_orders.trade_window import SettingsError, load_settings, parse_hhmm

MODES = ("manual", "semi", "auto")
FILTERS = ("squeeze_release", "rsi_extreme", "light_volume", "squeeze_4h1h")
EARNINGS_UNKNOWN = ("block", "allow")

DEFAULTS: dict[str, Any] = {
    "mode": "manual",
    "expire_sessions": 10,
    "max_enqueue": 15,
    "filters": dict.fromkeys(FILTERS, True),
    "earnings_unknown": "block",
    "earnings_cache_max_days": 30,
    "intraday_times": ["10:35", "11:35", "12:35", "13:35", "14:35", "15:35"],
}


def validate(data: dict) -> dict:
    """The section with defaults filled in, refusing values that are not valid."""
    if not isinstance(data, dict):
        raise SettingsError("the tradebot settings are not an object")
    out = {**DEFAULTS, **data, "filters": {**DEFAULTS["filters"], **(data.get("filters") or {})}}
    if out["mode"] not in MODES:
        raise SettingsError(f"tradebot mode {out['mode']!r} is not one of {', '.join(MODES)}")
    for key, low, high in (("expire_sessions", 1, 60), ("max_enqueue", 1, 50), ("earnings_cache_max_days", 1, 365)):
        v = out[key]
        if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
            raise SettingsError(f"tradebot {key} must be a whole number from {low} to {high}")
    unknown = set(out["filters"]) - set(FILTERS)
    if unknown or not all(isinstance(v, bool) for v in out["filters"].values()):
        raise SettingsError(f"tradebot filters must be on/off switches for {', '.join(FILTERS)}")
    if out["earnings_unknown"] not in EARNINGS_UNKNOWN:
        raise SettingsError(f"tradebot earnings_unknown must be {' or '.join(EARNINGS_UNKNOWN)}")
    times = out["intraday_times"]
    if not isinstance(times, list):
        raise SettingsError("tradebot intraday_times must be a list of HH:MM times")
    out["intraday_times"] = sorted({parse_hhmm(t, "intraday time").strftime("%H:%M") for t in times})
    return out


def load() -> dict:
    return validate(load_settings().get("tradebot") or {})
