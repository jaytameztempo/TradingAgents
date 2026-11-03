"""Put the router-allowed candidates for one date into the Tradebot queue.

Only the scan kinds whose regime matches the date's SPY+QQQ route label are queued:

- SIDE: the long and short names the earnings blackout allows (the confirmed, ranked, sector-checked channel
  members), in the ranked side handoff's order, carrying their newest channel report's numbers,
- UP: the members of the newest ``scan-up-category-<category>`` basket for the date, by score,
- DOWN: the same for ``scan-down-category-<category>``.

At most max_enqueue names per run. A name already active in the queue for the same bot is refreshed, not added
twice (store.enqueue). Nothing is fetched and no order is placed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from extensions.status.daily_status import LONG, SHORT, SIDE
from extensions.tradebot import sources, store
from extensions.tradebot.sources import DOWN, UP, SourceError


@dataclass
class EnqueueResult:
    as_of: date
    label: str
    route_file: str
    candidates: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    already_queued: list[str] = field(default_factory=list)
    capped: list[str] = field(default_factory=list)
    note: str = ""


def _label(kind: str, name: str) -> str:
    k, c = sources.KINDS[kind], sources.category_of(kind, name)
    return f"{k.label} (Category {c})" if c else k.label


def _item(kind: str, path_name: str, data: dict, ticker: str, as_of: date, expire_sessions: int) -> dict:
    k = sources.KINDS[kind]
    return store.new_item(ticker=ticker, as_of=as_of.isoformat(), kind=kind, scan=_label(kind, path_name),
                          scan_file=path_name, side=k.side, needs_regime=k.regime,
                          metrics=sources.member_metrics(kind, data, ticker), source="auto",
                          expire_sessions=expire_sessions)


def side_items(as_of: date, expire_sessions: int) -> list[dict]:
    """The blackout-allowed long and short names, in the ranked handoff's order, from their channel reports."""
    _, b = sources.blackout(as_of)
    handoff = sources.ranked_handoff(as_of)
    ranks = {s: (n or {}).get("rank") for s, n in ((handoff[1].get("names") or {}) if handoff else {}).items()}
    out: list[tuple[int, dict]] = []
    for side in (LONG, SHORT):
        names = b["allowed"][side]
        if not names:
            continue
        kind = sources.CHANNEL_KIND[side]
        found = sources.newest_scan(kind, None, as_of, on_or_before=False)
        if found is None:
            raise SourceError(f"the blackout allows {side} names but there is no {side} channel report for {as_of}")
        path, data = found
        passing = set(sources.passing_members(kind, data))
        for s in names:
            if s in passing:
                rank = ranks.get(s)
                out.append((rank if isinstance(rank, int) else 10_000, _item(kind, path.name, data, s, as_of,
                                                                              expire_sessions)))
    return [item for _, item in sorted(out, key=lambda r: r[0])]


def trend_items(kind: str, category: int, as_of: date, expire_sessions: int) -> list[dict]:
    found = sources.newest_scan(kind, category, as_of, on_or_before=False)
    if found is None:
        raise SourceError(f"no {sources.KINDS[kind].label} Category {category} basket for {as_of}")
    path, data = found
    return [_item(kind, path.name, data, s, as_of, expire_sessions) for s in sources.passing_members(kind, data)]


def enqueue_for_date(as_of: date, category: int = 1, max_enqueue: int = 15,
                     expire_sessions: int = store.EXPIRE_SESSIONS, dry_run: bool = False) -> EnqueueResult:
    route_path, route = sources.route(as_of)
    label = route["regime_label"]
    result = EnqueueResult(as_of, label, route_path.name)
    if label == SIDE:
        items = side_items(as_of, expire_sessions)
    elif label == UP:
        items = trend_items("up_cat", category, as_of, expire_sessions)
    else:
        items = trend_items("down_cat", category, as_of, expire_sessions)
    result.candidates = [i["ticker"] for i in items]
    kept, result.capped = items[:max_enqueue], [i["ticker"] for i in items[max_enqueue:]]
    if not kept:
        result.note = f"no {label} candidates for {as_of}"
        return result
    if dry_run:
        result.note = "dry run: nothing queued"
        return result
    done = store.enqueue("trade", kept)
    result.added, result.already_queued = done["added"], done["already_queued"]
    return result


__all__ = ["DOWN", "UP", "EnqueueResult", "enqueue_for_date", "side_items", "trend_items"]
