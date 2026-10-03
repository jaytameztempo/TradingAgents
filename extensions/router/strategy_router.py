"""Route both scan baskets by the SPY regime label for the same date.

- UP: UPBot is allowed for the upward-scan passers; DOWNBot is blocked.
- DOWN: DOWNBot is allowed for the breakdown-scan passers; UPBot is blocked.
- SIDE: both trend bots are blocked; passers from either scan are routed to SIDEBot.

The playbooks read ``tradeable`` and ``blocked``: under UP or DOWN only the
allowed bot's names are listed, in ``tradeable``; under SIDE every passer is
listed in ``blocked``. Each scan's passers are also kept in ``up_passed`` and
``down_passed`` so the file explains a blocked side as well as the allowed one.

The router reads the latest SPY label and the latest basket of each scan for
the date, by the ``created_at`` recorded inside each file. The basket for the
allowed side is required; under SIDE neither is. A missing label, a missing
required basket, or an unreadable candidate file raises and no decision is
made. It fetches no bars and places no orders.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from extensions.regime.regime_bot import DOWN, SIDE, UP, RegimeLabel, default_regime_dir, load_label
from extensions.scans.basket import TickerBasket, default_basket_dir, load_basket
from extensions.scans.breakdown_short_candidates import SCAN_NAME as DOWN_SCAN_NAME
from extensions.scans.upward_trend_momentum import SCAN_NAME as UP_SCAN_NAME

SERIES = "ALL"
REGIME_SYMBOL = "SPY"
# Fields added since version 1 are optional with defaults, so routes written
# before them still load and every version-1 reader still reads new routes.
SCHEMA_VERSION = 1


class MissingInput(RuntimeError):
    """The regime label or a required basket for the date is missing or unreadable."""


@dataclass(frozen=True)
class RouteDecision:
    as_of: date
    created_at: datetime
    series: str
    regime_symbol: str
    regime_label: str
    up_allowed: bool
    tradeable: list[str]
    blocked: list[str]
    reason: str
    basket_file: str | None  # the upward-scan basket
    regime_file: str
    schema_version: int = SCHEMA_VERSION
    down_allowed: bool = False
    up_passed: list[str] = field(default_factory=list)
    down_passed: list[str] = field(default_factory=list)
    down_basket_file: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> RouteDecision:
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported route schema_version: {data.get('schema_version')!r}")
        fields = dict(data)
        fields["as_of"] = date.fromisoformat(data["as_of"])
        fields["created_at"] = datetime.fromisoformat(data["created_at"])
        return cls(**fields)


def latest_basket(
    as_of: date, basket_dir: str | Path | None = None, scan_name: str = UP_SCAN_NAME
) -> tuple[Path, TickerBasket]:
    """The newest basket from scan_name for as_of."""
    folder = Path(basket_dir) if basket_dir is not None else default_basket_dir()
    found = _newest_basket(as_of, folder, scan_name)
    if found is None:
        raise MissingInput(f"no {scan_name} basket for {as_of} in {folder}")
    return found


def latest_regime(
    as_of: date, regime_dir: str | Path | None = None, symbol: str = REGIME_SYMBOL
) -> tuple[Path, RegimeLabel]:
    """The newest regime label for symbol and as_of."""
    folder = Path(regime_dir) if regime_dir is not None else default_regime_dir()
    found = [
        (path, label)
        for path, label in _load_all(folder, as_of, load_label, "regime label")
        if label.symbol == symbol and label.as_of == as_of
    ]
    if not found:
        raise MissingInput(f"no {symbol} regime label for {as_of} in {folder}")
    return max(found, key=lambda item: item[1].created_at)


def route(
    regime: RegimeLabel,
    regime_file: str | Path,
    up: tuple[str | Path, TickerBasket] | None = None,
    down: tuple[str | Path, TickerBasket] | None = None,
    created_at: datetime | None = None,
) -> RouteDecision:
    """Decide which trend bot, if any, may run, and on which names.

    ``up`` and ``down`` are (file, basket) for the upward and breakdown scans,
    or None when that scan has no basket for the date.
    """
    for scan_name, found in ((UP_SCAN_NAME, up), (DOWN_SCAN_NAME, down)):
        if found is not None and found[1].as_of != regime.as_of:
            raise ValueError(
                f"{scan_name} basket is for {found[1].as_of} but the regime label is for {regime.as_of}"
            )
    up_passed = up[1].tickers if up is not None else []
    down_passed = down[1].tickers if down is not None else []

    if regime.label == UP:
        _require(up, UP_SCAN_NAME, regime)
        tradeable, blocked = up_passed, []
        reason = f"{regime.symbol} regime is UP: UPBot is allowed for the upward-scan passers; DOWNBot is blocked"
    elif regime.label == DOWN:
        _require(down, DOWN_SCAN_NAME, regime)
        tradeable, blocked = down_passed, []
        reason = f"{regime.symbol} regime is DOWN: DOWNBot is allowed for the breakdown-scan passers; UPBot is blocked"
    elif regime.label == SIDE:
        tradeable, blocked = [], list(dict.fromkeys(up_passed + down_passed))
        reason = (
            f"{regime.symbol} regime is SIDE: UPBot and DOWNBot are blocked; "
            "passed names are routed to SIDEBot only"
        )
    else:
        raise ValueError(f"unknown regime label: {regime.label!r}")

    return RouteDecision(
        as_of=regime.as_of,
        created_at=created_at or datetime.now(UTC),
        series=SERIES,
        regime_symbol=regime.symbol,
        regime_label=regime.label,
        up_allowed=regime.label == UP,
        down_allowed=regime.label == DOWN,
        tradeable=tradeable,
        blocked=blocked,
        up_passed=up_passed,
        down_passed=down_passed,
        reason=reason,
        basket_file=str(up[0]) if up is not None else None,
        down_basket_file=str(down[0]) if down is not None else None,
        regime_file=str(regime_file),
    )


def route_for_date(
    as_of: date, basket_dir: str | Path | None = None, regime_dir: str | Path | None = None
) -> RouteDecision:
    """Load the latest SPY label and each scan's latest basket for as_of, and route them."""
    regime_path, regime = latest_regime(as_of, regime_dir)
    folder = Path(basket_dir) if basket_dir is not None else default_basket_dir()
    up = _newest_basket(as_of, folder, UP_SCAN_NAME)
    down = _newest_basket(as_of, folder, DOWN_SCAN_NAME)
    return route(regime, regime_path, up, down)


def default_route_dir() -> Path:
    """~/.tradingagents/routes, beside the baskets and regime labels and out of git."""
    return Path.home() / ".tradingagents" / "routes"


def save_decision(decision: RouteDecision, out_dir: str | Path | None = None) -> Path:
    """Write the decision as a new file and return its path. An existing decision is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_route_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"route_{decision.series}_{decision.as_of}_{decision.created_at:%Y%m%dT%H%M%SZ}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(decision.to_dict(), fh, indent=2)
        fh.write("\n")
    return path


def load_decision(path: str | Path) -> RouteDecision:
    return RouteDecision.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _require(found, scan_name: str, regime: RegimeLabel) -> None:
    if found is None:
        raise MissingInput(
            f"no {scan_name} basket for {regime.as_of}; it is required when the "
            f"{regime.symbol} regime is {regime.label}"
        )


def _newest_basket(as_of: date, folder: Path, scan_name: str) -> tuple[Path, TickerBasket] | None:
    """The newest basket from scan_name for as_of, or None if that scan wrote none."""
    found = [
        (path, basket)
        for path, basket in _load_all(folder, as_of, load_basket, "basket")
        if basket.scan_name == scan_name and basket.as_of == as_of
    ]
    return max(found, key=lambda item: item[1].created_at) if found else None


def _load_all(folder: Path, as_of: date, loader, kind: str):
    """Load every file named for as_of. One that cannot be read stops the route: skipping
    it could quietly pick an older file than the one that was meant to count."""
    for path in sorted(folder.glob(f"*_{as_of}_*.json")):
        try:
            yield path, loader(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MissingInput(f"cannot read {kind} file {path}: {exc}") from exc
