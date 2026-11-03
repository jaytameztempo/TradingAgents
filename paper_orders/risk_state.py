"""The risk guard's memory: the highest equity the paper account has shown, for the drawdown limit.

Kept in <data home>/risk_state.json (``extensions.tradebot.store.data_home``: TRADINGAGENTS_UI_HOME, else
~/.tradingagents), written atomically, and refused inside a OneDrive-synced folder like the queue. A file that
cannot be read is an error, never a fresh start: a lost peak would quietly reset the drawdown limit.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from extensions.tradebot import store

STATE_VERSION = 1


class RiskStateError(RuntimeError):
    """The risk state file cannot be read or written."""


def state_path() -> Path:
    return store.data_home() / "risk_state.json"


def load() -> dict:
    """The saved state, or an empty one when no file exists yet."""
    path = state_path()
    if not path.exists():
        return {"state_version": STATE_VERSION, "peak_equity": None, "peak_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RiskStateError(f"cannot read {path}: {exc}; fix or delete it on purpose to reset the peak") from exc
    if not isinstance(data, dict) or data.get("state_version") != STATE_VERSION:
        raise RiskStateError(f"{path} is not a version {STATE_VERSION} risk state file")
    return data


def save(data: dict) -> None:
    try:
        store.assert_not_synced()
    except store.StoreError as exc:
        raise RiskStateError(str(exc)) from exc
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def record_equity(state: dict, equity: float, at: datetime | None = None) -> dict:
    """The state with the peak raised to equity when it is higher (not saved)."""
    peak = state.get("peak_equity")
    if peak is None or equity > peak:
        return {**state, "state_version": STATE_VERSION, "peak_equity": round(equity, 2),
                "peak_at": (at or datetime.now(UTC)).isoformat(timespec="seconds")}
    return state
