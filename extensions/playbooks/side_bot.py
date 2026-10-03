"""SIDEBot: the sideways / mean-reversion playbook. It writes a research plan only.

It reads the newest route decision for a date and writes a plan for one routed
symbol only when the route's regime label is SIDE. An UP, DOWN, or any other
label stops it before anything is written.

The route file is read with plain ``json`` rather than through
``extensions.router``: that package imports the Alpaca SDK, which loads Alpaca's
trading client with it. Nothing here imports Alpaca, fetches bars, or places an order.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

SIDE = "SIDE"
BOT_NAME = "SIDEBot"
# The route schema this bot understands; matches strategy_router.SCHEMA_VERSION.
ROUTE_SCHEMA_VERSION = 1
# The same plain-US-ticker rule as extensions.market_data.alpaca_bars.validate_ticker.
_TICKER = re.compile(r"^[A-Z]{1,5}(?:[.-][A-Z]{1,2})?$")

MEAN_REVERSION_NOTE = (
    "The market is labeled SIDE, so SIDEBot may plan a fade. "
    "Fade extremes only inside a defined range. If the range is not clean, prefer cash. "
    "SIDE is permission to plan a fade, not an obligation to trade."
)


class MissingRoute(RuntimeError):
    """The route for the date is missing or unreadable."""


class RegimeNotSide(RuntimeError):
    """The route's regime label is not SIDE, so SIDEBot may not plan."""


class SymbolNotInRoute(ValueError):
    """The symbol is neither tradeable nor blocked in the route, so it was never routed."""


@dataclass(frozen=True)
class Route:
    """The fields of a saved route decision that SIDEBot reads."""

    as_of: date
    created_at: datetime
    regime_symbol: str
    regime_label: str
    tradeable: list[str]
    blocked: list[str]

    @classmethod
    def from_dict(cls, data: dict) -> Route:
        if data.get("schema_version") != ROUTE_SCHEMA_VERSION:
            raise ValueError(f"unsupported route schema_version: {data.get('schema_version')!r}")
        return cls(
            as_of=date.fromisoformat(data["as_of"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            regime_symbol=str(data["regime_symbol"]),
            regime_label=str(data["regime_label"]),
            tradeable=list(data["tradeable"]),
            blocked=list(data["blocked"]),
        )

    @property
    def routed(self) -> list[str]:
        return self.tradeable + self.blocked


@dataclass(frozen=True)
class SidePlan:
    symbol: str
    as_of: date
    regime_label: str
    regime_symbol: str
    route_file: str
    created_at: datetime


def validate_symbol(symbol: str) -> str:
    """Return the symbol upper-cased, or raise ValueError if it is not a plain US ticker."""
    cleaned = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not _TICKER.fullmatch(cleaned):
        raise ValueError(f"not a valid US ticker: {symbol!r}")
    return cleaned


def default_route_dir() -> Path:
    return Path.home() / ".tradingagents" / "routes"


def default_plan_dir() -> Path:
    """~/.tradingagents/plans, beside the routes and out of git."""
    return Path.home() / ".tradingagents" / "plans"


def latest_route(as_of: date, route_dir: str | Path | None = None) -> tuple[Path, Route]:
    """The newest route decision for as_of, by the created_at inside each file.

    One unreadable file stops the run: skipping it could quietly pick an older
    route than the one that was meant to count.
    """
    folder = Path(route_dir) if route_dir is not None else default_route_dir()
    found = []
    for path in sorted(folder.glob(f"*_{as_of}_*.json")):
        try:
            route = Route.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MissingRoute(f"cannot read route file {path}: {exc}") from exc
        if route.as_of == as_of:
            found.append((path, route))
    if not found:
        raise MissingRoute(f"no route decision for {as_of} in {folder}")
    return max(found, key=lambda item: item[1].created_at)


def make_plan(
    route: Route, route_file: str | Path, symbol: str, created_at: datetime | None = None
) -> SidePlan:
    """A plan for a routed symbol. The regime is checked first: anything but SIDE stops here."""
    if route.regime_label != SIDE:
        raise RegimeNotSide(f"regime is {route.regime_label}")
    symbol = validate_symbol(symbol)
    if symbol not in route.routed:
        routed = ", ".join(route.routed) or "none"
        raise SymbolNotInRoute(f"{symbol} is not in the route for {route.as_of} (routed tickers: {routed})")
    return SidePlan(
        symbol=symbol,
        as_of=route.as_of,
        regime_label=route.regime_label,
        regime_symbol=route.regime_symbol,
        route_file=str(route_file),
        created_at=created_at or datetime.now(UTC),
    )


def render(plan: SidePlan) -> str:
    return "\n".join([
        f"{BOT_NAME} plan (research only)",
        f"symbol: {plan.symbol}",
        f"date: {plan.as_of.isoformat()}",
        f"regime: {plan.regime_label} ({plan.regime_symbol})",
        f"route file: {plan.route_file}",
        f"mean-reversion note: {MEAN_REVERSION_NOTE}",
        "no order placed.",
        "",
    ])


def save_plan(plan: SidePlan, out_dir: str | Path | None = None) -> Path:
    """Write the plan as a new file and return its path. An existing plan is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_plan_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"sidebot_{plan.symbol}_{plan.as_of}_{plan.created_at:%Y%m%dT%H%M%SZ}.md"
    with path.open("x", encoding="utf-8") as fh:
        fh.write(render(plan))
    return path
