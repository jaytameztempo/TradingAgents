"""Hand the sideways channel members to research, gated by the route's market regime.

It reads saved files only: the newest SCAN-LongSidewaysChannel report, the newest
SCAN-ShortSidewaysChannel report and the newest route decision for the date,
each picked by the ``created_at`` inside the file (SCANBot.md section 9):

- the route's market label is SIDE: every long channel member is PLAN as a long
  fade off support, every short channel member is PLAN as a short fade off
  resistance, and a research plan is written for each that ends with
  "no order placed."
- the label is UP or DOWN: every member is BLOCKED and no plan is written.

The channel scans, router rules and bots are not touched. It fetches no bars,
calls no Alpaca client and places no orders.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from extensions.research.handoff import BLOCKED, PLAN, PLAN_ENDING, default_handoff_dir, default_plan_dir
from extensions.research.research_runner import latest_route
from extensions.router.strategy_router import MissingInput, RouteDecision
from extensions.scanbot import long_channel, short_channel
from extensions.scanbot.funnel import default_report_dir

SIDE_HANDOFF_VERSION = 1
SIDE = "SIDE"
LONG_FADE, SHORT_FADE = "LONG_FADE", "SHORT_FADE"
SETUPS = {
    LONG_FADE: "long fade off support",
    SHORT_FADE: "short fade off resistance",
}


@dataclass(frozen=True)
class SideName:
    basket: str  # the channel scan name
    setup: str  # LONG_FADE or SHORT_FADE
    status: str
    reasons: list[str]
    channel_detail: list[str]
    levels: dict
    quality: dict[str, bool | None]
    plan_file: str | None = None


@dataclass(frozen=True)
class SideHandoff:
    as_of: date
    created_at: datetime
    long_channel_report: str
    short_channel_report: str
    route_file: str
    regime_symbol: str
    regime_label: str
    regime_labels: dict[str, str | None]
    names: dict[str, dict]
    planned: list[str]
    blocked: list[str]
    warnings: list[str] = field(default_factory=list)
    orders_placed: bool = False
    side_handoff_version: int = SIDE_HANDOFF_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data


def _latest(prefix: str, loader, as_of: date, report_dir: str | Path | None):
    """The newest report named ``{prefix}_{as_of}_*.json``, by the created_at inside each file."""
    folder = Path(report_dir) if report_dir is not None else default_report_dir()
    found = []
    for path in sorted(folder.glob(f"{prefix}_{as_of}_*.json")):
        try:
            report = loader(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MissingInput(f"cannot read {prefix} report {path}: {exc}") from exc
        if report.as_of == as_of:
            found.append((path, report))
    if not found:
        raise MissingInput(f"no {prefix} report for {as_of} in {folder}")
    return max(found, key=lambda item: item[1].created_at)


def latest_long_channel(as_of: date, report_dir: str | Path | None = None):
    return _latest("long_channel", long_channel.load_report, as_of, report_dir)


def latest_short_channel(as_of: date, report_dir: str | Path | None = None):
    return _latest("short_channel", short_channel.load_report, as_of, report_dir)


def _levels(metrics: dict, setup: str) -> dict:
    keep = ("close", "support", "resistance", "height_pct", "adx_14", "atr_14", "atr_pct",
            "distance_to_support_atr", "distance_to_resistance_atr", "rsi_14", "last_bar_date",
            "shortable", "easy_to_borrow")
    levels = {k: metrics[k] for k in keep if k in metrics}
    levels["support_touches"] = len(metrics.get("support_touches", []))
    levels["resistance_touches"] = len(metrics.get("resistance_touches", []))
    atr, support, resistance = metrics.get("atr_14"), metrics.get("support"), metrics.get("resistance")
    if atr is not None and support is not None and resistance is not None:
        # The scan's own break threshold is 0.25 ATR through the line being faded.
        if setup == LONG_FADE:
            levels["invalidation"] = round(support - long_channel.BREAK_ATR_MAX * atr, 4)
            levels["far_side"] = resistance
        else:
            levels["invalidation"] = round(resistance + short_channel.BREAK_ATR_MAX * atr, 4)
            levels["far_side"] = support
        levels["midline"] = round((support + resistance) / 2, 4)
    return levels


def build_side_handoff(
    long_report: long_channel.LongChannelReport,
    long_path: str | Path,
    short_report: short_channel.ShortChannelReport,
    short_path: str | Path,
    decision: RouteDecision,
    route_path: str | Path,
    created_at: datetime | None = None,
) -> SideHandoff:
    """Decide PLAN or BLOCKED for each channel member. Writes nothing."""
    for name, report in (("long channel", long_report), ("short channel", short_report)):
        if report.as_of != decision.as_of:
            raise ValueError(f"the {name} report is for {report.as_of} but the route is for {decision.as_of}")
    regime = decision.regime_label
    names: dict[str, dict] = {}
    for report, setup in ((long_report, LONG_FADE), (short_report, SHORT_FADE)):
        for symbol in report.members:
            if symbol in names:
                raise ValueError(f"{symbol} is in both the long and the short channel basket")
            call = report.calls[symbol]
            if regime == SIDE:
                status, reasons = PLAN, [f"market regime SIDE allows a {SETUPS[setup]}"]
            else:
                status, reasons = BLOCKED, [f"market regime {regime} is not SIDE; no {SETUPS[setup]}"]
            names[symbol] = asdict(SideName(
                basket=report.scan_name,
                setup=setup,
                status=status,
                reasons=reasons,
                channel_detail=list(call["detail"]),
                levels=_levels(call["metrics"], setup),
                quality=dict(call["quality"]),
            ))

    warnings = list(dict.fromkeys(long_report.warnings + short_report.warnings))
    return SideHandoff(
        as_of=decision.as_of,
        created_at=created_at or datetime.now(UTC),
        long_channel_report=str(long_path),
        short_channel_report=str(short_path),
        route_file=str(route_path),
        regime_symbol=decision.regime_symbol,
        regime_label=regime,
        regime_labels=dict(decision.regime_labels),
        names=names,
        planned=[s for s, n in names.items() if n["status"] == PLAN],
        blocked=[s for s, n in names.items() if n["status"] == BLOCKED],
        warnings=warnings,
    )


def plan_text(handoff: SideHandoff, symbol: str) -> str:
    """The research plan for a PLAN name. Its last line is always PLAN_ENDING."""
    name = handoff.names[symbol]
    if name["status"] != PLAN:
        raise ValueError(f"{symbol} is {name['status']}; it gets no plan")
    setup, lv = name["setup"], name["levels"]
    long = setup == LONG_FADE
    line, far = ("support", "resistance") if long else ("resistance", "support")
    report = handoff.long_channel_report if long else handoff.short_channel_report
    distance = lv.get("distance_to_support_atr" if long else "distance_to_resistance_atr")
    lines = [
        f"# Research plan: {symbol} ({SETUPS[setup]}) as of {handoff.as_of}",
        "",
        f"- Market regime: {handoff.regime_label} ({handoff.regime_symbol})",
        f"- Basket: {name['basket']}",
        f"- Setup: {SETUPS[setup]}, inside the 60-session channel only",
        f"- Channel report: {report}",
        f"- Route: {handoff.route_file}",
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


def save_side_handoff(
    handoff: SideHandoff, out_dir: str | Path | None = None, plan_dir: str | Path | None = None
) -> tuple[Path, list[Path]]:
    """Write one plan per PLAN name, then the handoff. Existing files are never overwritten."""
    stamp = f"{handoff.as_of}_{handoff.created_at:%Y%m%dT%H%M%SZ}"
    plans = []
    if handoff.planned:
        pfolder = Path(plan_dir) if plan_dir is not None else default_plan_dir()
        pfolder.mkdir(parents=True, exist_ok=True)
        for symbol in handoff.planned:
            path = pfolder / f"side_research_{symbol}_{stamp}.md"
            with path.open("x", encoding="utf-8") as fh:
                fh.write(plan_text(handoff, symbol))
            handoff.names[symbol]["plan_file"] = str(path)
            plans.append(path)
    folder = Path(out_dir) if out_dir is not None else default_handoff_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"side_handoff_{stamp}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(handoff.to_dict(), fh, indent=2)
        fh.write("\n")
    return path, plans


def side_handoff_for_date(
    as_of: date, report_dir: str | Path | None = None, route_dir: str | Path | None = None
) -> SideHandoff:
    long_path, long_report = latest_long_channel(as_of, report_dir)
    short_path, short_report = latest_short_channel(as_of, report_dir)
    route_path, decision = latest_route(as_of, route_dir)
    return build_side_handoff(long_report, long_path, short_report, short_path, decision, route_path)
