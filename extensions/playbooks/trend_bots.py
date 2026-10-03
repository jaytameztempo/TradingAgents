"""UPBot and DOWNBot: the trend-following playbooks. Each writes a research plan only.

A bot writes a plan for one routed symbol only when the newest route for the
date carries its own regime label: UP for UPBot, DOWN for DOWNBot. Any other
label stops it before anything is written.

Routes are read through SIDEBot's plain-JSON reader, so nothing here imports
Alpaca, fetches bars, or places an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from extensions.playbooks.side_bot import (
    MissingRoute,
    Route,
    SymbolNotInRoute,
    default_plan_dir,
    latest_route,
    validate_symbol,
)

__all__ = [
    "DOWNBOT",
    "UPBOT",
    "MissingRoute",
    "RegimeMismatch",
    "SymbolNotInRoute",
    "TrendBot",
    "TrendPlan",
    "latest_route",
    "make_plan",
    "render",
    "save_plan",
    "validate_symbol",
]


@dataclass(frozen=True)
class TrendBot:
    name: str
    regime: str
    note: str


UPBOT = TrendBot(
    name="UPBot",
    regime="UP",
    note=(
        "The market is labeled UP, so UPBot may plan a long entry. "
        "Enter on pullbacks in an established uptrend, not on the first green bar. "
        "Do not fade strength. Do not run mean-reversion shorts."
    ),
)

DOWNBOT = TrendBot(
    name="DOWNBot",
    regime="DOWN",
    note=(
        "The market is labeled DOWN, so DOWNBot may plan a short, a hedge, or cash. "
        "Short failed rallies, or reduce and hedge. "
        "Do not buy dips just because price looks cheap while the down structure is intact."
    ),
)


class RegimeMismatch(RuntimeError):
    """The route's regime label is not the bot's own, so the bot may not plan."""


@dataclass(frozen=True)
class TrendPlan:
    bot: TrendBot
    symbol: str
    as_of: date
    regime_label: str
    regime_symbol: str
    route_file: str
    created_at: datetime


def make_plan(
    bot: TrendBot, route: Route, route_file: str | Path, symbol: str, created_at: datetime | None = None
) -> TrendPlan:
    """A plan for a routed symbol. The regime is checked first: a mismatch is a hard reject."""
    if route.regime_label != bot.regime:
        raise RegimeMismatch(f"regime is {route.regime_label}")
    symbol = validate_symbol(symbol)
    if symbol not in route.routed:
        routed = ", ".join(route.routed) or "none"
        raise SymbolNotInRoute(f"{symbol} is not in the route for {route.as_of} (routed tickers: {routed})")
    return TrendPlan(
        bot=bot,
        symbol=symbol,
        as_of=route.as_of,
        regime_label=route.regime_label,
        regime_symbol=route.regime_symbol,
        route_file=str(route_file),
        created_at=created_at or datetime.now(UTC),
    )


def render(plan: TrendPlan) -> str:
    return "\n".join([
        f"{plan.bot.name} plan (research only)",
        f"symbol: {plan.symbol}",
        f"date: {plan.as_of.isoformat()}",
        f"regime: {plan.regime_label} ({plan.regime_symbol})",
        f"route file: {plan.route_file}",
        f"trend-following note: {plan.bot.note}",
        "no order placed.",
        "",
    ])


def save_plan(plan: TrendPlan, out_dir: str | Path | None = None) -> Path:
    """Write the plan as a new file and return its path. An existing plan is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_plan_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{plan.bot.name.lower()}_{plan.symbol}_{plan.as_of}_{plan.created_at:%Y%m%dT%H%M%SZ}"
    path = folder / f"{stem}.md"
    with path.open("x", encoding="utf-8") as fh:
        fh.write(render(plan))
    return path
