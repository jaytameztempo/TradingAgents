"""Hand the ranked SIDE channel names to research under a SIDE market. It places no orders.

It reads saved files only:

- the newest channel rank report for the date, picked by the ``created_at`` inside each file,
- the long and short channel reports that rank report names in its sources,
- the newest SPY+QQQ route for the date.

The route's market label must be SIDE, or nothing is built. Every long name
the ranker emitted is PLAN as a long fade off support and every short name it
kept is PLAN as a short fade off resistance, each in rank order, with a
research plan that ends with "no order placed." A long name the ranker dropped
(sector cap or name cap) is NOT_RANKED and gets no plan. A ranked name with no
operator-supplied sector stops the build: no sector is invented here.

The channel scans, ranker, router rules and bots are not touched. It fetches
no bars, calls no Alpaca client and places no orders.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from extensions.research.handoff import PLAN, PLAN_ENDING, default_handoff_dir, default_plan_dir
from extensions.research.side_handoff import LONG_FADE, SETUPS, SHORT_FADE, SIDE
from extensions.research.side_handoff import _levels as channel_levels
from extensions.router.strategy_router import MissingInput
from extensions.scanbot import long_channel, short_channel
from extensions.scanbot.channel_rank import (
    DROPPED_MAX_NAMES,
    DROPPED_SECTOR_CAP,
    EMITTED,
    KEPT_SHORT,
    LONG_SCAN,
    MAX_NAMES,
    SECTOR_CAP,
    SHORT_SCAN,
)
from extensions.scanbot.funnel import default_report_dir
from extensions.scanbot.sector_map import UNKNOWN_SECTOR
from extensions.status.daily_status import MARKET, StatusInputError, latest_route

NOT_RANKED = "NOT_RANKED"
LONG, SHORT = "long", "short"
SETUP = {LONG: LONG_FADE, SHORT: SHORT_FADE}
BASKET = {LONG: LONG_SCAN, SHORT: SHORT_SCAN}
PRIOR_PLANS_NOTE = "earlier side_research_* and ranked_side_research_* plans are left in place, unchanged"


@dataclass(frozen=True)
class RankedSideHandoff:
    as_of: date
    created_at: datetime
    rank_report: str
    long_channel_report: str
    short_channel_report: str
    route_file: str
    regime_symbol: str
    regime_label: str
    prior_plans_note: str
    planned_long: list[str]
    planned_short: list[str]
    not_ranked: list[str]
    names: dict[str, dict]
    warnings: list[str] = field(default_factory=list)
    orders_placed: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data


def latest_channel_rank(as_of: date, report_dir: str | Path | None = None) -> tuple[Path, dict]:
    """The newest ``channel_rank_{as_of}_*.json``, by the created_at inside each file."""
    folder = Path(report_dir) if report_dir is not None else default_report_dir()
    found = []
    for path in sorted(folder.glob(f"channel_rank_{as_of}_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(data["created_at"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MissingInput(f"cannot read channel rank report {path}: {exc}") from exc
        if data.get("as_of") == as_of.isoformat():
            found.append((created, path, data))
    if not found:
        raise MissingInput(f"no channel rank report for {as_of} in {folder}")
    _, path, data = max(found, key=lambda item: item[0])
    return path, data


def _not_ranked_reason(row: dict) -> str:
    status = row["status"]
    if status == DROPPED_SECTOR_CAP:
        why = f"{row['sector']} already has {SECTOR_CAP} emitted long names (cap {SECTOR_CAP})"
    elif status == DROPPED_MAX_NAMES:
        why = f"{MAX_NAMES} long names were already emitted (cap {MAX_NAMES})"
    else:
        why = "not emitted by the ranker"
    return f"ranker status {status}: {why}; no plan written"


def _check_members(side: str, members: list[str], rows: list[dict]) -> None:
    ranked = [r["symbol"] for r in rows]
    if sorted(members) != sorted(ranked):
        raise ValueError(f"the {side} channel members {sorted(members)} are not the ranked {side} names {sorted(ranked)}")


def build_ranked_side_handoff(
    rank: dict,
    rank_path: str | Path,
    long_report: long_channel.LongChannelReport,
    long_path: str | Path,
    short_report: short_channel.ShortChannelReport,
    short_path: str | Path,
    route: dict,
    route_path: str | Path,
    created_at: datetime | None = None,
) -> RankedSideHandoff:
    """Decide PLAN or NOT_RANKED for each ranked name. Writes nothing."""
    label = route.get("regime_label")
    if label != SIDE:
        raise ValueError(f"market label is {label}, not SIDE; no ranked side plans")
    as_of = date.fromisoformat(rank["as_of"])
    for name, found in (("route", date.fromisoformat(route["as_of"])),
                        ("long channel report", long_report.as_of), ("short channel report", short_report.as_of)):
        if found != as_of:
            raise ValueError(f"the {name} is for {found} but the channel rank report is for {as_of}")
    rows = {LONG: sorted(rank["long_ranked"], key=lambda r: r["rank"]),
            SHORT: sorted(rank["short_ranked"], key=lambda r: r["rank"])}
    unmapped = [r["symbol"] for side in (LONG, SHORT) for r in rows[side] if r.get("sector") in (None, UNKNOWN_SECTOR)]
    if unmapped:
        raise ValueError(f"names with no operator-supplied sector: {', '.join(unmapped)}; "
                         "add them to extensions/scanbot/sector_map.py and rank again")
    reports = {LONG: long_report, SHORT: short_report}
    for side in (LONG, SHORT):
        _check_members(side, reports[side].members, rows[side])

    planned: dict[str, list[str]] = {LONG: [], SHORT: []}
    names: dict[str, dict] = {}
    not_ranked: dict[str, dict] = {}
    for side, plan_status in ((LONG, EMITTED), (SHORT, KEPT_SHORT)):
        setup = SETUP[side]
        for row in rows[side]:
            symbol = row["symbol"]
            if symbol in names or symbol in not_ranked:
                raise ValueError(f"{symbol} is ranked on both the long and the short side")
            if row["status"] != plan_status:
                not_ranked[symbol] = {
                    "side": side, "setup": setup, "status": NOT_RANKED, "reason": _not_ranked_reason(row),
                    "rank": row["rank"], "score": row["score"], "sector": row["sector"],
                    "rank_status": row["status"], "plan_file": None,
                }
                continue
            call = reports[side].calls[symbol]
            names[symbol] = {
                "side": side, "setup": setup, "setup_text": SETUPS[setup], "status": PLAN,
                "rank": row["rank"], "score": row["score"], "sector": row["sector"],
                "rank_status": row["status"], "components": dict(row["components"]),
                "levels": channel_levels(call["metrics"], setup), "quality": dict(call["quality"]),
                "channel_detail": list(call["detail"]), "plan_file": None,
            }
            planned[side].append(symbol)
    names.update(not_ranked)

    return RankedSideHandoff(
        as_of=as_of,
        created_at=created_at or datetime.now(UTC),
        rank_report=str(rank_path),
        long_channel_report=str(long_path),
        short_channel_report=str(short_path),
        route_file=str(route_path),
        regime_symbol=route["regime_symbol"],
        regime_label=label,
        prior_plans_note=PRIOR_PLANS_NOTE,
        planned_long=planned[LONG],
        planned_short=planned[SHORT],
        not_ranked=list(not_ranked),
        names=names,
        warnings=list(dict.fromkeys(rank.get("warnings", []))),
    )


def plan_text(handoff: RankedSideHandoff, symbol: str) -> str:
    """The research plan for a PLAN name. Its last line is always PLAN_ENDING."""
    name = handoff.names[symbol]
    if name["status"] != PLAN:
        raise ValueError(f"{symbol} is {name['status']}; it gets no plan")
    side, setup, lv = name["side"], name["setup"], name["levels"]
    long = side == LONG
    line, far = ("support", "resistance") if long else ("resistance", "support")
    report = handoff.long_channel_report if long else handoff.short_channel_report
    distance = lv.get("distance_to_support_atr" if long else "distance_to_resistance_atr")
    if long:
        scored = sum(1 for n in handoff.names.values() if n["side"] == LONG)
        standing = f"long rank {name['rank']} of {scored} scored ({len(handoff.planned_long)} emitted)"
    else:
        standing = f"short rank {name['rank']} of {len(handoff.planned_short)} kept"
    lines = [
        f"# Research plan: {symbol} ({SETUPS[setup]}) as of {handoff.as_of}",
        "",
        f"- Market regime: {handoff.regime_label} ({handoff.regime_symbol})",
        f"- Basket: {BASKET[side]}",
        f"- Setup: {SETUPS[setup]}, inside the 60-session channel only",
        f"- Channel report: {report}",
        f"- Route: {handoff.route_file}",
        "",
        "## Rank",
        f"- {standing}, score {name['score']} / 100, sector {name['sector']}",
        f"- Components: {', '.join(f'{k} {v}' for k, v in name['components'].items())}",
        f"- Ranker status: {name['rank_status']}",
        f"- Rank report: {handoff.rank_report}",
        "",
        "## Channel",
        *(f"- {d}" for d in name["channel_detail"]),
        "",
        "## Reference levels (from the scan, not orders)",
        f"- Close {lv.get('close')} on {lv.get('last_bar_date')}, {distance:+.2f} ATR from {line}"
        if distance is not None else f"- Close {lv.get('close')} on {lv.get('last_bar_date')}",
        f"- Support {lv.get('support')}, resistance {lv.get('resistance')}, midline {lv.get('midline')}",
        f"- ATR(14) {lv.get('atr_14')} ({lv.get('atr_pct')}% of close), ADX(14) {lv.get('adx_14')}",
        f"- Invalidation: a close past {lv.get('invalidation')} (0.25 ATR through {line})",
        f"- Far side of the channel: {lv.get('far_side')} ({far})",
        "",
        "## Quality checks (recorded by the scan, not required)",
        *(f"- {q}: {v}" for q, v in name["quality"].items()),
        "",
        "## Research steps",
        f"1. Review the daily channel for {symbol}: are the {line} touches clean, and is the range still intact?",
        "2. Check news, filings and earnings dates around the as-of date; an event can break a range.",
    ]
    if long:
        lines.append("3. Check liquidity at support and whether the last bars show buyers defending it.")
    else:
        lines.append("3. Check liquidity and borrow availability now; the scan's borrow flags are not point in time.")
    lines += [
        "4. Write down the invalidation level and the reason the fade would fail.",
        "5. SIDE is permission to plan a fade, not an obligation to trade. If the range is not clean, prefer cash.",
        "",
        "Research only; " + PLAN_ENDING,
    ]
    text = "\n".join(lines) + "\n"
    assert text.rstrip().endswith(PLAN_ENDING)
    return text


def save_ranked_side_handoff(
    handoff: RankedSideHandoff, out_dir: str | Path | None = None, plan_dir: str | Path | None = None
) -> tuple[Path, list[Path]]:
    """Write one plan per PLAN name, then the handoff. Existing files are never overwritten."""
    stamp = f"{handoff.as_of}_{handoff.created_at:%Y%m%dT%H%M%SZ}"
    plans = []
    planned = handoff.planned_long + handoff.planned_short
    if planned:
        pfolder = Path(plan_dir) if plan_dir is not None else default_plan_dir()
        pfolder.mkdir(parents=True, exist_ok=True)
        for symbol in planned:
            path = pfolder / f"ranked_side_research_{symbol}_{stamp}.md"
            with path.open("x", encoding="utf-8") as fh:
                fh.write(plan_text(handoff, symbol))
            handoff.names[symbol]["plan_file"] = str(path)
            plans.append(path)
    folder = Path(out_dir) if out_dir is not None else default_handoff_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"ranked_side_handoff_{stamp}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(handoff.to_dict(), fh, indent=2)
        fh.write("\n")
    return path, plans


def ranked_side_handoff_for_date(
    as_of: date, report_dir: str | Path | None = None, route_dir: str | Path | None = None
) -> RankedSideHandoff:
    rank_path, rank = latest_channel_rank(as_of, report_dir)
    sources = rank.get("sources") or {}
    try:
        long_path, short_path = Path(sources["long_channel"]), Path(sources["short_channel"])
        long_report, short_report = long_channel.load_report(long_path), short_channel.load_report(short_path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MissingInput(f"cannot read the channel reports named in {rank_path}: {exc!r}") from exc
    try:
        route_path, route = latest_route(as_of, route_dir)
    except StatusInputError as exc:
        raise MissingInput(f"no usable {MARKET} route: {exc}") from exc
    return build_ranked_side_handoff(rank, rank_path, long_report, long_path, short_report, short_path,
                                     route, route_path)
