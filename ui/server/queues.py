"""Work queues fed from the Scans page ("Send to"), and permanent deletes ("Trash").

Queues (research, strategy, trade) live in ~/.tradingagents/queues.json. Each item is built here from the
scan file on disk, never from what the browser sends, so a queued row always carries the scan's own numbers.

Trash is permanent, as the operator chose: a whole scan file is deleted, or one ticker is removed from the
scan file itself. In a channel report the removed ticker's call is also marked not passed, so the ranker,
the side handoffs and the ticket builder (which check calls as well as members) skip it too.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ui.server import files, paths

QUEUES = ("research", "strategy", "trade")

# Scan kind on the Scans page -> (folder, file-name prefix, side, regime a trade on that side needs).
SCAN_KINDS = {
    "up": (paths.baskets_dir, "scan-upward-trend-momentum", "long", "UP"),
    "down": (paths.baskets_dir, "scan-breakdown-short-candidates", "short", "DOWN"),
    "long": (paths.scanbot_dir, "long_channel", "long", "SIDE"),
    "short": (paths.scanbot_dir, "short_channel", "short", "SIDE"),
}
SCAN_LABEL = {"up": "Upward trend momentum", "down": "Breakdown short candidates",
              "long": "Long sideways channel", "short": "Short sideways channel"}
REMOVED_REASON = "removed_by_operator"
_lock = threading.Lock()


class QueueError(ValueError):
    """Bad input: unknown scan kind or queue, a file that is not that kind's scan, or a ticker not in it."""


def _queue_path() -> Path:
    return paths.data_home() / "queues.json"


def _read_queues() -> dict[str, list[dict]]:
    path = _queue_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    return {q: list(data.get(q) or []) for q in QUEUES}


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def scan_file(kind: str, name: str) -> Path:
    """The scan file `name` of `kind`, refusing anything else."""
    if kind not in SCAN_KINDS:
        raise QueueError(f"unknown scan kind {kind!r}")
    folder_fn, prefix, _, _ = SCAN_KINDS[kind]
    if not name.startswith(prefix + "_"):
        raise QueueError(f"{name} is not a {SCAN_LABEL[kind]} file")
    try:
        return files.safe_child(folder_fn(), name)
    except files.NotFound:
        raise QueueError(f"no such scan file: {name}") from None


def _read_scan(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QueueError(f"cannot read {path.name}: {exc}") from None


def _members(kind: str, data: dict) -> dict[str, dict]:
    """ticker -> the small set of numbers a queued row shows."""
    out: dict[str, dict] = {}
    if kind in ("up", "down"):
        for m in data.get("members") or []:
            met = m.get("metrics") or {}
            out[m["ticker"]] = {"close": met.get("close"), "sma_50": met.get("sma_50"),
                                "sma_200": met.get("sma_200"), "score": m.get("score")}
    else:
        calls = data.get("calls") or {}
        for t in data.get("members") or []:
            met = (calls.get(t) or {}).get("metrics") or {}
            out[t] = {k: met.get(k) for k in ("close", "support", "resistance", "height_pct", "adx_14", "atr_14")}
    return out


def send(queue: str, kind: str, name: str, tickers: list[str] | None) -> dict:
    """Queue some (or, with no tickers, all) members of a scan file. Already-queued rows are not added twice."""
    if queue not in QUEUES:
        raise QueueError(f"unknown queue {queue!r}")
    path = scan_file(kind, name)
    data = _read_scan(path)
    members = _members(kind, data)
    wanted = list(dict.fromkeys(tickers)) if tickers else list(members)
    missing = [t for t in wanted if t not in members]
    if missing:
        raise QueueError(f"not passing members of {name}: {', '.join(missing)}")
    _, _, side, regime = SCAN_KINDS[kind]
    as_of = data.get("as_of")
    with _lock:
        queues = _read_queues()
        have = {(i["ticker"], i["as_of"], i["kind"]) for i in queues[queue]}
        added = []
        for t in wanted:
            if (t, as_of, kind) in have:
                continue
            item = {"id": uuid.uuid4().hex[:12], "ticker": t, "as_of": as_of, "kind": kind,
                    "scan": SCAN_LABEL[kind], "scan_file": name, "side": side, "needs_regime": regime,
                    "metrics": members[t], "added_at": datetime.now(UTC).isoformat(timespec="seconds")}
            queues[queue].append(item)
            added.append(t)
        _write_json(_queue_path(), queues)
    return {"queue": queue, "added": added, "already_queued": [t for t in wanted if t not in added]}


def remove_item(queue: str, item_id: str) -> bool:
    if queue not in QUEUES:
        raise QueueError(f"unknown queue {queue!r}")
    with _lock:
        queues = _read_queues()
        before = len(queues[queue])
        queues[queue] = [i for i in queues[queue] if i.get("id") != item_id]
        _write_json(_queue_path(), queues)
    return len(queues[queue]) < before


def _regime_for(as_of: str | None) -> tuple[str | None, str | None]:
    """(label, source) for a date: the newest route's label, else the newest regime file's."""
    if not as_of:
        return None, None
    route = files.latest(paths.routes_dir(), as_of=as_of)
    if route and isinstance(route.get("data"), dict) and route["data"].get("regime_label"):
        d = route["data"]
        return d["regime_label"], f"route {route['file']} ({d.get('regime_symbol')})"
    regime = files.latest(paths.regimes_dir(), as_of=as_of)
    if regime and isinstance(regime.get("data"), dict):
        return regime["data"].get("label"), f"regime {regime['file']}"
    return None, None


def _route_status(ticker: str, as_of: str | None) -> str:
    route = files.latest(paths.routes_dir(), as_of=as_of) if as_of else None
    d = (route or {}).get("data") or {}
    if ticker in (d.get("tradeable") or []):
        return "ALLOWED"
    if ticker in (d.get("blocked") or []):
        return "BLOCKED"
    return "NOT_ROUTED" if route else "NO_ROUTE"


def list_queues() -> dict[str, list[dict]]:
    """Every queue, newest first, with live checks: route status (research) and regime verdict (strategy)."""
    queues = _read_queues()
    regimes: dict[str | None, tuple] = {}
    for item in queues["research"]:
        item["route_status"] = _route_status(item["ticker"], item.get("as_of"))
    for item in queues["strategy"]:
        as_of = item.get("as_of")
        if as_of not in regimes:
            regimes[as_of] = _regime_for(as_of)
        label, source = regimes[as_of]
        item["regime_label"], item["regime_source"] = label, source
        need = item.get("needs_regime")
        if label is None:
            item["verdict"], item["reason"] = "NO_REGIME", f"no regime label or route for {as_of}"
        elif label == need:
            item["verdict"], item["reason"] = "ALLOWED", f"{item['side']} {item['scan']} needs {need}; regime is {label}"
        else:
            item["verdict"], item["reason"] = "BLOCKED", f"{item['side']} {item['scan']} needs {need}; regime is {label}"
    return {q: list(reversed(items)) for q, items in queues.items()}


# --- trash (permanent) -----------------------------------------------------------------

def delete_scan(kind: str, name: str) -> None:
    scan_file(kind, name).unlink()


def remove_ticker(kind: str, name: str, ticker: str) -> dict:
    """Remove one ticker from the scan file itself. A channel report also marks its call not passed."""
    path = scan_file(kind, name)
    data = _read_scan(path)
    if kind in ("up", "down"):
        members = data.get("members") or []
        rejected = data.get("rejected") or []
        kept = [m for m in members if m.get("ticker") != ticker]
        kept_rejected = [r for r in rejected if r.get("ticker") != ticker]
        if len(kept) == len(members) and len(kept_rejected) == len(rejected):
            raise QueueError(f"{ticker} is not in {name}")
        data["members"], data["rejected"] = kept, kept_rejected
    else:
        members = data.get("members") or []
        if ticker not in members:
            raise QueueError(f"{ticker} is not a member of {name}")
        data["members"] = [t for t in members if t != ticker]
        call = (data.get("calls") or {}).get(ticker)
        if isinstance(call, dict):
            call["passed"] = False
            call["reasons"] = [*(call.get("reasons") or []), REMOVED_REASON]
            call["detail"] = [*(call.get("detail") or []), "removed on the Scans page"]
    _write_json(path, data)
    return {"file": name, "removed": ticker, "members_left": len(data["members"])}
