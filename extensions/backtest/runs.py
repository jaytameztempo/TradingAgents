"""Saved backtest runs: one JSON file per run under <home>/backtest/runs, and exports as JSON or CSV.

A saved run is the full backtest result (params, summary, curve, trades, skipped, days) as it was when saved, with
a name and the time it was saved. Later rebuilds or newly cached bars do not change it.
"""

from __future__ import annotations

import csv
import io
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

RUNS_DIR = Path("backtest") / "runs"
RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
MAX_NAME = 120
FORMATS = ("json", "trades_csv", "summary_csv")

TRADE_COLUMNS = [
    "origin", "run_date", "bot", "symbol", "side", "levels_source", "reference_entry", "entry_date", "entry", "stop",
    "target", "exit_date", "exit", "exit_reason", "r", "pnl_usd", "qty", "notional", "risk_dollars", "fees",
    "risk_per_share", "sessions_held", "entry_type", "sizing", "capped", "calm_scale", "calm_scale_fallback",
    "outlier", "report_as_of", "known_on", "source_file", "note", "detail",
]
SUMMARY_COLUMNS = [
    "origin", "strategy", "label", "trades", "open", "bars_end", "wins", "win_rate", "avg_r", "total_r",
    "total_r_ex_best", "total_return_pct", "total_return_usd", "total_return_usd_ex_best", "max_drawdown_pct",
    "peak_positions", "peak_open_risk_pct", "outliers",
]


class RunNotFound(LookupError):
    """No saved run with that id."""


def runs_dir(home: str | Path) -> Path:
    return Path(home) / RUNS_DIR


def _path(home: str | Path, run_id: str) -> Path:
    if not RUN_ID.match(run_id or ""):
        raise RunNotFound(run_id)
    path = runs_dir(home) / f"{run_id}.json"
    if not path.is_file():
        raise RunNotFound(run_id)
    return path


def _meta(run: dict) -> dict:
    result = run.get("result") or {}
    totals = [{k: row.get(k) for k in ("origin", "trades", "open", "win_rate", "avg_r", "total_r",
                                        "total_return_pct", "total_return_usd")}
              for row in result.get("summary") or [] if row.get("strategy") == "all"]
    return {"id": run["id"], "name": run.get("name") or "", "saved_at": run.get("saved_at"),
            "params": result.get("params") or {}, "totals": totals, "trades": len(result.get("trades") or [])}


def save_run(home: str | Path, result: dict, name: str = "", now: datetime | None = None) -> dict:
    """Write ``result`` as a new saved run; returns its listing entry."""
    now = now or datetime.now(timezone.utc)
    run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
    p = result.get("params") or {}
    name = (name or "").strip()[:MAX_NAME] or f"{p.get('start')} to {p.get('end')} · {p.get('strategy', 'all')}"
    run = {"id": run_id, "name": name, "saved_at": now.isoformat(timespec="seconds"), "result": result}
    folder = runs_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{run_id}.tmp"
    tmp.write_text(json.dumps(run), encoding="utf-8")
    tmp.replace(folder / f"{run_id}.json")
    return _meta(run)


def list_runs(home: str | Path) -> list[dict]:
    """Saved runs, newest first. Unreadable files are skipped."""
    folder = runs_dir(home)
    out = []
    for path in sorted(folder.glob("*.json"), reverse=True) if folder.is_dir() else []:
        if not RUN_ID.match(path.stem):
            continue
        try:
            out.append(_meta(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, KeyError):
            continue
    return out


def load_run(home: str | Path, run_id: str) -> dict:
    return json.loads(_path(home, run_id).read_text(encoding="utf-8"))


def delete_run(home: str | Path, run_id: str) -> None:
    _path(home, run_id).unlink()


def rename_run(home: str | Path, run_id: str, name: str) -> dict:
    """Give a saved run a new name; its results are not touched."""
    path = _path(home, run_id)
    run = json.loads(path.read_text(encoding="utf-8"))
    run["name"] = (name or "").strip()[:MAX_NAME] or run["name"]
    tmp = path.with_name(f".{run_id}.tmp")
    tmp.write_text(json.dumps(run), encoding="utf-8")
    tmp.replace(path)
    return _meta(run)


def _csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: "" if row.get(k) is None else row.get(k) for k in columns})
    return buf.getvalue()


def export_run(run: dict, fmt: str) -> tuple[str, str, str]:
    """(body, media type, file name) for one saved run as ``json``, ``trades_csv`` or ``summary_csv``."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", run.get("name") or "").strip("-")[:60] or "backtest"
    base = f"backtest_{slug}_{run['id']}"
    result = run.get("result") or {}
    if fmt == "json":
        return json.dumps(run, indent=2), "application/json", f"{base}.json"
    if fmt == "trades_csv":
        return _csv(result.get("trades") or [], TRADE_COLUMNS), "text/csv", f"{base}_trades.csv"
    return _csv(result.get("summary") or [], SUMMARY_COLUMNS), "text/csv", f"{base}_summary.csv"
