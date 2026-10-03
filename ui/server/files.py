"""Read the JSON and Markdown files the extension scripts and TradingAgents write.

Files are read with plain ``json`` so this process never imports the Alpaca SDK
through ``extensions.router`` (the same choice extensions/playbooks/side_bot.py makes).
Nothing here writes, renames or deletes a file.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

# {prefix}_{YYYY-MM-DD}_{YYYYmmddTHHMMSSZ}.{json|md}, as every extension script names its output.
_STAMPED = re.compile(r"^(?P<prefix>.+)_(?P<as_of>\d{4}-\d{2}-\d{2})_(?P<stamp>\d{8}T\d{6}Z)\.(?:json|md)$")
# Report folders from TradingAgentsGraph.save_reports: {TICKER}_{YYYYmmdd_HHMMSS}.
_REPORT_DIR = re.compile(r"^(?P<ticker>[A-Z0-9.\-]{1,12})_(?P<stamp>\d{8}_\d{6})$")
_STATE_LOG = re.compile(r"^full_states_log_(?P<date>\d{4}-\d{2}-\d{2})\.json$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._\-]+$")


class NotFound(LookupError):
    """The requested file is not there, or its name is not one this UI serves."""


def safe_child(folder: Path, name: str) -> Path:
    """folder/name, refusing anything that is not a plain file name inside folder."""
    if not _SAFE_NAME.fullmatch(name) or name in (".", ".."):
        raise NotFound(name)
    path = (folder / name).resolve()
    if path.parent != folder.resolve() or not path.is_file():
        raise NotFound(name)
    return path


def _created_at(data: Any) -> str:
    try:
        return datetime.fromisoformat(data["created_at"]).isoformat()
    except (KeyError, TypeError, ValueError):
        return ""


def read_json_file(path: Path) -> dict[str, Any]:
    """One artifact as {file, path, prefix, as_of, data}, or with "error" if it cannot be read."""
    match = _STAMPED.match(path.name)
    entry: dict[str, Any] = {
        "file": path.name,
        "path": str(path),
        "prefix": match["prefix"] if match else None,
        "as_of": match["as_of"] if match else None,
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        entry["error"] = f"cannot read: {exc}"
        return entry
    entry["data"] = data
    if isinstance(data, dict) and data.get("as_of"):
        entry["as_of"] = data["as_of"]
    return entry


def list_artifacts(folder: Path, prefix: str | None = None, as_of: str | None = None) -> list[dict]:
    """Every JSON artifact in folder, newest first (by as_of, then created_at inside the file)."""
    if not folder.is_dir():
        return []
    entries = []
    for path in folder.glob("*.json"):
        match = _STAMPED.match(path.name)
        if prefix and not (match and match["prefix"].startswith(prefix)):
            continue
        if as_of and not (match and match["as_of"] == as_of):
            continue
        entries.append(read_json_file(path))
    return sorted(
        entries,
        key=lambda e: (e.get("as_of") or "", _created_at(e.get("data")), e["file"]),
        reverse=True,
    )


def latest(folder: Path, as_of: str | None = None, prefix: str | None = None) -> dict | None:
    """The newest readable artifact, using the router's rule: newest created_at for the date."""
    readable = [e for e in list_artifacts(folder, prefix, as_of) if "data" in e]
    return readable[0] if readable else None


def artifact_dates(*folders: Path) -> list[str]:
    """Distinct as_of dates in the folders' file names, newest first."""
    found: set[str] = set()
    for folder in folders:
        if folder.is_dir():
            found |= {m["as_of"] for p in folder.iterdir() if (m := _STAMPED.match(p.name))}
    return sorted(found, reverse=True)


# --- playbook plans (.md) -------------------------------------------------------------

def parse_plan(path: Path) -> dict[str, Any]:
    """A plan file's title line and its 'key: value' lines, plus the raw text."""
    match = _STAMPED.match(path.name)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    fields: dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(": ")
        if sep:
            fields[key.strip()] = value.strip()
    bot, _, symbol = (match["prefix"] if match else "").partition("_")
    return {
        "file": path.name,
        "path": str(path),
        "bot": bot or None,
        "symbol": symbol or fields.get("symbol"),
        "as_of": match["as_of"] if match else fields.get("date"),
        "stamp": match["stamp"] if match else None,
        "title": lines[0] if lines else "",
        "fields": fields,
        "text": text,
    }


def list_plans(folder: Path) -> list[dict]:
    if not folder.is_dir():
        return []
    plans = [parse_plan(p) for p in folder.glob("*.md")]
    return sorted(plans, key=lambda p: (p["as_of"] or "", p["stamp"] or "", p["file"]), reverse=True)


# --- TradingAgents research output ----------------------------------------------------

def list_state_logs(results: Path) -> list[dict]:
    """One entry per full_states_log_{date}.json: ticker, date and the final rating."""
    if not results.is_dir():
        return []
    runs = []
    for path in results.glob("*/TradingAgentsStrategy_logs/full_states_log_*.json"):
        match = _STATE_LOG.match(path.name)
        if not match:
            continue
        entry: dict[str, Any] = {
            "ticker": path.parents[1].name,
            "date": match["date"],
            "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        }
        try:
            entry["rating"] = json.loads(path.read_text(encoding="utf-8")).get("final_rating")
        except (OSError, ValueError, AttributeError) as exc:
            entry["error"] = f"cannot read: {exc}"
        runs.append(entry)
    return sorted(runs, key=lambda r: (r["date"], r["modified"]), reverse=True)


def read_state_log(results: Path, ticker: str, date: str) -> dict:
    if not re.fullmatch(r"[A-Z0-9.\-]{1,12}", ticker) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise NotFound(f"{ticker} {date}")
    path = safe_child(results / ticker / "TradingAgentsStrategy_logs", f"full_states_log_{date}.json")
    return json.loads(path.read_text(encoding="utf-8"))


def list_report_dirs(results: Path) -> list[dict]:
    reports = results / "reports"
    if not reports.is_dir():
        return []
    found = []
    for folder in reports.iterdir():
        match = _REPORT_DIR.match(folder.name)
        if folder.is_dir() and match:
            created = datetime.strptime(match["stamp"], "%Y%m%d_%H%M%S").isoformat()
            found.append({"name": folder.name, "ticker": match["ticker"], "created": created})
    return sorted(found, key=lambda r: r["created"], reverse=True)


def read_report_dir(results: Path, name: str) -> dict:
    """Every Markdown file in one report folder, keyed by its path relative to the folder."""
    if not _REPORT_DIR.match(name):
        raise NotFound(name)
    reports = (results / "reports").resolve()
    folder = (reports / name).resolve()
    if folder.parent != reports or not folder.is_dir():
        raise NotFound(name)
    sections = {
        path.relative_to(folder).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(folder.rglob("*.md"))
    }
    return {"name": name, "sections": sections}
