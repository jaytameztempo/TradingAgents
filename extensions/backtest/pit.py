"""Point-in-time reading of the saved pipeline files.

Every saved file is named ``<kind>_<as_of>_<YYYYMMDDTHHMMSSZ>.json``; the stamp is when it was written. A file is
*known on* the New York calendar date of that stamp, and it is visible on a run date D only when it was known on or
before D. Among the visible files of one kind the newest wins: the latest as-of first, then the latest stamp. A file
written later (a re-run) is invisible on earlier run dates.

Files are read with plain ``json`` and never written.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

MARKET_TZ = "America/New_York"
_NAME = re.compile(r"_(\d{4}-\d{2}-\d{2})_(\d{8}T\d{6}Z)\.json$")

# kind -> (folder under the data home, file-name globs). The first glob ranks above the others on the same as-of.
KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "route": ("routes", ("route_*.json",)),
    "side_handoff": ("handoffs", ("ranked_side_handoff_*.json", "side_handoff_*.json")),
    "trend_handoff": ("handoffs", ("handoff_*.json",)),
    "long_channel": ("scanbot", ("long_channel_*.json",)),
    "short_channel": ("scanbot", ("short_channel_*.json",)),
}


def known_on(created_at: datetime) -> date:
    """The New York calendar date a file written at ``created_at`` first existed on."""
    stamp = pd.Timestamp(created_at)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(UTC)
    return stamp.tz_convert(MARKET_TZ).date()


def parse_name(path: Path) -> tuple[date, datetime] | None:
    """(as_of, created_at) from a saved file's name, or None when the name does not follow the pattern."""
    match = _NAME.search(path.name)
    if not match:
        return None
    try:
        return (date.fromisoformat(match.group(1)),
                datetime.strptime(match.group(2), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC))
    except ValueError:
        return None


@dataclass
class Saved:
    """One saved file. ``data`` is read on first use."""

    path: Path
    as_of: date
    created_at: datetime
    rank: int = 0  # lower ranks above higher on the same as-of (ranked side handoff over the plain one)
    _data: dict | None = field(default=None, repr=False)

    @property
    def known_on(self) -> date:
        return known_on(self.created_at)

    @property
    def data(self) -> dict:
        if self._data is None:
            with self.path.open(encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError(f"{self.path.name} is not a JSON object")
            self._data = loaded
        return self._data


class Library:
    """The saved files under one data home, indexed by kind. Read-only."""

    def __init__(self, home: str | Path):
        self.home = Path(home)
        self._index: dict[str, list[Saved]] = {}
        self._by_name: dict[tuple[str, str], Saved | None] = {}
        self.warnings: list[str] = []

    def files(self, kind: str) -> list[Saved]:
        if kind not in self._index:
            folder, patterns = KINDS[kind]
            found: dict[Path, Saved] = {}
            for rank, pattern in enumerate(patterns):
                for path in sorted((self.home / folder).glob(pattern)):
                    parsed = parse_name(path)
                    if parsed and path not in found:
                        found[path] = Saved(path, parsed[0], parsed[1], rank)
            self._index[kind] = list(found.values())
        return self._index[kind]

    def newest(self, kind: str, on: date, accept: Callable[[Saved], bool] | None = None) -> Saved | None:
        """The newest file of ``kind`` visible on ``on`` (known on or before it) that ``accept`` takes."""
        visible = [s for s in self.files(kind) if s.known_on <= on]
        visible.sort(key=lambda s: (s.as_of, -s.rank, s.created_at), reverse=True)
        for saved in visible:
            try:
                if accept is None or accept(saved):
                    return saved
            except (OSError, ValueError) as exc:
                self._warn(f"{saved.path.name} could not be read ({exc}); passed over")
        return None

    def referenced(self, folder: str, recorded_path: str | None, on: date) -> Saved | None:
        """A file another file names (by its recorded path), looked up by name under ``folder`` in this data home.
        None when it is missing, unreadable, or was not yet known on ``on``."""
        if not recorded_path:
            return None
        name = Path(str(recorded_path).replace("\\", "/")).name
        key = (folder, name)
        if key not in self._by_name:
            path = self.home / folder / name
            parsed = parse_name(path)
            self._by_name[key] = Saved(path, parsed[0], parsed[1]) if parsed and path.is_file() else None
        saved = self._by_name[key]
        if saved is None or saved.known_on > on:
            return None
        try:
            saved.data  # noqa: B018 - read now so a bad file is reported once and treated as missing
        except (OSError, ValueError) as exc:
            self._warn(f"{name} could not be read ({exc}); treated as missing")
            self._by_name[key] = None
            return None
        return saved

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def pivot_confirmed(saved: Saved) -> bool:
    """A channel report that ran the pivot confirmation, the only kind the live tickets use."""
    return "pivot_confirmed" in (saved.data.get("stage_counts") or {})


# --- saved vs rebuilt dates (light: the rebuild module itself is only loaded by the rebuild job) -------------

RECON_DIR = Path("backtest") / "reconstructed"
MANIFEST = "manifest.json"
MANIFEST_VERSION = 1
DONE, HOLIDAY = "done", "holiday"


def recon_home(home: str | Path) -> Path:
    """Where rebuilt reports live, in the live folder layout."""
    return Path(home) / RECON_DIR


def read_manifest(home: str | Path) -> dict:
    """``{"version", "dates": {iso: {status, label, files, warnings, built_at}}}``; empty when there is none."""
    try:
        data = json.loads((recon_home(home) / MANIFEST).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("dates"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": MANIFEST_VERSION, "dates": {}}


def saved_dates(home: str | Path) -> set[date]:
    """As-of dates with a saved (live) route: these are replayed, not rebuilt."""
    found = set()
    for path in (Path(home) / "routes").glob("route_*.json"):
        parsed = parse_name(path)
        if parsed:
            found.add(parsed[0])
    return found


def rebuilt_dates(home: str | Path) -> set[date]:
    return {date.fromisoformat(d) for d, e in read_manifest(home)["dates"].items() if e.get("status") == DONE}


def weekdays(start: date, end: date) -> list[date]:
    return [d.date() for d in pd.bdate_range(start=pd.Timestamp(start), end=pd.Timestamp(end))]
