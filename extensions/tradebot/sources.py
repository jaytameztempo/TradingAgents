"""The saved files the Tradebot reads: routes, category baskets, channel reports, the earnings blackout.

Every file is read with plain ``json`` from folders under ``store.data_home()``. A file that cannot be read is an
error, never an empty answer, so every check that depends on one fails closed. Nothing here imports Alpaca.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from extensions.status.daily_status import LONG, MARKET, SHORT, SIDE
from extensions.tradebot import store

UP, DOWN = "UP", "DOWN"
CATEGORIES = (1, 2, 3)


@dataclass(frozen=True)
class Kind:
    """A queue item's scan kind: where its scan files live, their name prefix, its side and the label it needs."""
    folder: str  # under data home
    prefix: str  # file-name prefix; category kinds add -<category>
    side: str
    regime: str
    label: str
    basket: bool  # a TickerBasket (members with metrics) rather than a channel report (members + calls)
    category: bool


# The kinds the Tradebot checks (as ui.server.queues.SCAN_KINDS; the retired five-name scans are not here).
KINDS = {
    "up_cat": Kind("baskets", "scan-up-category", LONG, UP, "Category upward", True, True),
    "down_cat": Kind("baskets", "scan-down-category", SHORT, DOWN, "Category downward", True, True),
    "side_long_cat": Kind("baskets", "scan-side-long-category", LONG, SIDE, "Category sideways long", True, True),
    "side_short_cat": Kind("baskets", "scan-side-short-category", SHORT, SIDE, "Category sideways short", True, True),
    "long": Kind("scanbot", "long_channel", LONG, SIDE, "Long sideways channel", False, False),
    "short": Kind("scanbot", "short_channel", SHORT, SIDE, "Short sideways channel", False, False),
}
CHANNEL_KIND = {LONG: "long", SHORT: "short"}


class SourceError(RuntimeError):
    """A file the Tradebot needs is missing or cannot be read."""


def folder(name: str) -> Path:
    return store.data_home() / name


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SourceError(f"{path} is not a JSON object")
    return data


def _created(path: Path, data: dict) -> datetime:
    try:
        return datetime.fromisoformat(data["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError(f"{path} has no usable created_at: {exc}") from exc


def newest(where: Path, pattern: str, as_of: date, on_or_before: bool = False) -> tuple[Path, dict] | None:
    """The newest file matching pattern (a glob with one {date} field) for as_of, or with on_or_before the newest
    dated on or before it: by as_of, then by the created_at inside. None when there is none."""
    found = []
    glob = pattern.format(date="*" if on_or_before else as_of.isoformat())
    for path in sorted(where.glob(glob)):
        data = _load(path)
        try:
            d = date.fromisoformat(data.get("as_of") or "")
        except ValueError as exc:
            raise SourceError(f"{path} has no usable as_of") from exc
        if d == as_of or (on_or_before and d < as_of):
            found.append((d, _created(path, data), path, data))
    if not found:
        return None
    _, _, path, data = max(found, key=lambda f: (f[0], f[1]))
    return path, data


def find_route(as_of: date) -> tuple[Path, dict] | None:
    """The newest SPY+QQQ route for as_of, or None. A route that cannot be read, or has no known label, is an
    error."""
    found = None
    for path in sorted(folder("routes").glob(f"route_*_{as_of}_*.json")):
        data = _load(path)
        if data.get("as_of") != as_of.isoformat() or data.get("regime_symbol") != MARKET:
            continue
        if found is None or _created(path, data) > _created(*found):
            found = (path, data)
    if found is not None and found[1].get("regime_label") not in (UP, DOWN, SIDE):
        raise SourceError(f"route {found[0].name} has unknown label {found[1].get('regime_label')!r}")
    return found


def route(as_of: date) -> tuple[Path, dict]:
    """The newest SPY+QQQ route for as_of. Missing is an error."""
    found = find_route(as_of)
    if found is None:
        raise SourceError(f"no {MARKET} route for {as_of} in {folder('routes')}")
    return found


def route_on_or_before(as_of: date, days: int = 7) -> tuple[Path, dict] | None:
    """The newest SPY+QQQ route dated as_of or up to `days` calendar days before (an intraday check uses the last
    close's route)."""
    for back in range(days):
        found = find_route(as_of - timedelta(days=back))
        if found is not None:
            return found
    return None


def category_of(kind: str, name: str) -> int | None:
    k = KINDS[kind]
    if not k.category:
        return 0 if name.startswith(k.prefix + "_") else None
    return next((c for c in CATEGORIES if name.startswith(f"{k.prefix}-{c}_")), None)


def scan_pattern(kind: str, category: int | None) -> str:
    k = KINDS[kind]
    return f"{k.prefix}-{category}_{{date}}_*.json" if k.category else f"{k.prefix}_{{date}}_*.json"


def newest_scan(kind: str, category: int | None, as_of: date, on_or_before: bool = True) -> tuple[Path, dict] | None:
    """The newest scan file of this kind (and category) dated as_of, or on or before it."""
    return newest(folder(KINDS[kind].folder), scan_pattern(kind, category), as_of, on_or_before)


def passing_members(kind: str, data: dict) -> list[str]:
    """The names a scan file passes, in its own order (baskets are sorted by score already)."""
    if KINDS[kind].basket:
        return [m["ticker"] for m in data.get("members") or [] if isinstance(m, dict) and m.get("ticker")]
    calls = data.get("calls") or {}
    return [s for s in data.get("members") or [] if (calls.get(s) or {}).get("passed") is True]


def member_metrics(kind: str, data: dict, ticker: str) -> dict:
    """The numbers a queued row carries for one name: its scan metrics (and score for a basket)."""
    if KINDS[kind].basket:
        m = next((m for m in data.get("members") or [] if m.get("ticker") == ticker), None) or {}
        return {**(m.get("metrics") or {}), "score": m.get("score")}
    return dict(((data.get("calls") or {}).get(ticker) or {}).get("metrics") or {})


def blackout(as_of: date) -> tuple[Path, dict]:
    found = newest(folder("status"), "earnings_blackout_{date}_*.json", as_of)
    if found is None:
        raise SourceError(f"no earnings blackout for {as_of} in {folder('status')}")
    allowed = found[1].get("allowed")
    if not isinstance(allowed, dict) or not all(isinstance(allowed.get(s), list) for s in (LONG, SHORT)):
        raise SourceError(f"earnings blackout {found[0].name} has no allowed long and short lists")
    return found


def ranked_handoff(as_of: date) -> tuple[Path, dict] | None:
    return newest(folder("handoffs"), "ranked_side_handoff_{date}_*.json", as_of)
