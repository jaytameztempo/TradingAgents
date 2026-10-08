"""Hand the 4-hour confirmed names to research, gated by the route's market regime.

It reads saved files only: the newest SCANBot 4-hour report and the newest route
decision for the date, each picked by the ``created_at`` inside the file. For
every confirmed name:

- the route's market label equals the name's side (UP for an UP name, DOWN for
  a DOWN name): status PLAN, and a research plan is written that ends with
  "no order placed."
- anything else, including a SIDE market: status BLOCKED and no plan.
- a name the operator marks delisted is BLOCKED with no plan, whatever the regime.

The route supplies only its market label; its own passers are recorded, not
re-routed. The daily gates, router rules and bots are not touched. It fetches no
bars, calls no Alpaca client and places no orders.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from extensions.research.research_runner import latest_route
from extensions.router.strategy_router import MissingInput, RouteDecision
from extensions.scanbot.confirm_4h import Confirm4hReport, load_report
from extensions.scanbot.funnel import default_report_dir
from extensions.scanbot.pivot import SIDES

HANDOFF_VERSION = 1
PLAN, BLOCKED = "PLAN", "BLOCKED"
PLAN_ENDING = "no order placed."
DELISTED_SOURCE = "operator"


@dataclass(frozen=True)
class NameHandoff:
    side: str
    status: str
    reasons: list[str]
    confirm_detail: list[str]
    delisted: dict | None = None
    plan_file: str | None = None


@dataclass(frozen=True)
class Handoff:
    as_of: date
    created_at: datetime
    confirm4h_report: str
    route_file: str
    regime_symbol: str
    regime_label: str
    regime_labels: dict[str, str | None]
    names: dict[str, dict]
    planned: list[str]
    blocked: list[str]
    route_passers_not_confirmed: list[str]
    warnings: list[str] = field(default_factory=list)
    orders_placed: bool = False
    handoff_version: int = HANDOFF_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data


def parse_delisted(items: list[str] | None) -> dict[str, date]:
    """``["QRVO:2026-10-05"]`` -> ``{"QRVO": date(2026, 10, 5)}``."""
    out = {}
    for item in items or []:
        symbol, sep, when = item.partition(":")
        symbol = symbol.strip().upper()
        if not sep or not symbol:
            raise ValueError(f"--delisted must be SYMBOL:YYYY-MM-DD, not {item!r}")
        try:
            out[symbol] = date.fromisoformat(when.strip())
        except ValueError:
            raise ValueError(f"--delisted date in {item!r} is not YYYY-MM-DD") from None
    return out


def latest_confirm4h(as_of: date, report_dir: str | Path | None = None) -> tuple[Path, Confirm4hReport]:
    """The newest 4-hour report for as_of, by the created_at inside each file."""
    folder = Path(report_dir) if report_dir is not None else default_report_dir()
    found = []
    for path in sorted(folder.glob(f"confirm4h_{as_of}_*.json")):
        try:
            report = load_report(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MissingInput(f"cannot read 4-hour report {path}: {exc}") from exc
        if report.as_of == as_of:
            found.append((path, report))
    if not found:
        raise MissingInput(f"no 4-hour report for {as_of} in {folder}")
    return max(found, key=lambda item: item[1].created_at)


def build_handoff(
    report: Confirm4hReport,
    report_path: str | Path,
    decision: RouteDecision,
    route_path: str | Path,
    delisted: dict[str, date] | None = None,
    created_at: datetime | None = None,
) -> Handoff:
    """Decide PLAN or BLOCKED for each confirmed name. Writes nothing."""
    if report.as_of != decision.as_of:
        raise ValueError(f"the 4-hour report is for {report.as_of} but the route is for {decision.as_of}")
    delisted = delisted or {}
    regime = decision.regime_label
    names: dict[str, dict] = {}
    for side in SIDES:
        for symbol in report.confirmed.get(side, []):
            if symbol in names:
                raise ValueError(f"{symbol} is confirmed on both sides")
            reasons = []
            gone = None
            if symbol in delisted:
                gone = {"after": delisted[symbol].isoformat(), "source": DELISTED_SOURCE}
                reasons.append(f"delisted after {delisted[symbol]}; no plan")
            if regime != side:
                reasons.append(f"market regime {regime} does not match {side}")
            status = BLOCKED if reasons else PLAN
            if status == PLAN:
                reasons.append(f"market regime {regime} matches {side}")
            names[symbol] = asdict(NameHandoff(
                side=side,
                status=status,
                reasons=reasons,
                confirm_detail=list(report.calls[side][symbol]["detail"]),
                delisted=gone,
            ))

    routed = list(dict.fromkeys(decision.up_passed + decision.down_passed + decision.tradeable + decision.blocked))
    warnings = list(report.warnings)
    missing = [s for s in delisted if s not in names]
    if missing:
        warnings.append(f"--delisted names not in the confirmed list: {', '.join(missing)}")
    return Handoff(
        as_of=report.as_of,
        created_at=created_at or datetime.now(UTC),
        confirm4h_report=str(report_path),
        route_file=str(route_path),
        regime_symbol=decision.regime_symbol,
        regime_label=regime,
        regime_labels=dict(decision.regime_labels),
        names=names,
        planned=[s for s, n in names.items() if n["status"] == PLAN],
        blocked=[s for s, n in names.items() if n["status"] == BLOCKED],
        route_passers_not_confirmed=[s for s in routed if s not in names],
        warnings=warnings,
    )


def plan_text(handoff: Handoff, symbol: str) -> str:
    """The research plan for a PLAN name. Its last line is always PLAN_ENDING."""
    name = handoff.names[symbol]
    if name["status"] != PLAN:
        raise ValueError(f"{symbol} is {name['status']}; it gets no plan")
    side = name["side"]
    lean = "long (pullback after the swing high)" if side == "UP" else "short (bounce after the swing low)"
    lines = [
        f"# Research plan: {symbol} ({side}) as of {handoff.as_of}",
        "",
        f"- Market regime: {handoff.regime_label} ({handoff.regime_symbol}), matching {side}",
        f"- Setup: {lean}",
        f"- 4-hour report: {handoff.confirm4h_report}",
        f"- Route: {handoff.route_file}",
        "",
        "## 4-hour confirmation",
        *(f"- {d}" for d in name["confirm_detail"]),
        "",
        "## Research steps",
        f"1. Review the daily pivot and 4-hour structure for {symbol} as of {handoff.as_of}.",
        "2. Check news, filings and earnings dates around the as-of date.",
        "3. Check liquidity and, for a short, borrow availability.",
        "4. Write down the invalidation level and the reason the setup would fail.",
        "",
        "Research only; " + PLAN_ENDING,
    ]
    text = "\n".join(lines) + "\n"
    assert text.rstrip().endswith(PLAN_ENDING)
    return text


def default_handoff_dir() -> Path:
    return Path.home() / ".tradingagents" / "handoffs"


def default_plan_dir() -> Path:
    return Path.home() / ".tradingagents" / "plans"


def save_handoff(
    handoff: Handoff, out_dir: str | Path | None = None, plan_dir: str | Path | None = None
) -> tuple[Path, list[Path]]:
    """Write one plan per PLAN name, then the handoff. Existing files are never overwritten."""
    stamp = f"{handoff.as_of}_{handoff.created_at:%Y%m%dT%H%M%SZ}"
    plans = []
    if handoff.planned:
        pfolder = Path(plan_dir) if plan_dir is not None else default_plan_dir()
        pfolder.mkdir(parents=True, exist_ok=True)
        for symbol in handoff.planned:
            path = pfolder / f"research_{symbol}_{stamp}.md"
            with path.open("x", encoding="utf-8") as fh:
                fh.write(plan_text(handoff, symbol))
            handoff.names[symbol]["plan_file"] = str(path)
            plans.append(path)
    folder = Path(out_dir) if out_dir is not None else default_handoff_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"handoff_{stamp}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(handoff.to_dict(), fh, indent=2)
        fh.write("\n")
    return path, plans


def handoff_for_date(
    as_of: date,
    delisted: dict[str, date] | None = None,
    report_dir: str | Path | None = None,
    route_dir: str | Path | None = None,
) -> Handoff:
    report_path, report = latest_confirm4h(as_of, report_dir)
    route_path, decision = latest_route(as_of, route_dir)
    return build_handoff(report, report_path, decision, route_path, delisted)
