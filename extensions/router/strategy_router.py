"""Route the upward-trend basket by the SPY regime label for the same date.

- UP: UP playbooks are allowed for the basket's passed tickers.
- SIDE or DOWN: UP playbooks are blocked; the passed tickers are not tradeable under UP.

The router reads the latest basket and the latest SPY label for the date, by the
``created_at`` recorded inside each file. If either is missing, or a candidate
file cannot be read, it raises and no decision is made. It fetches no bars and
places no orders.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from extensions.regime.regime_bot import DOWN, SIDE, UP, RegimeLabel, default_regime_dir, load_label
from extensions.scans.basket import TickerBasket, default_basket_dir, load_basket
from extensions.scans.upward_trend_momentum import SCAN_NAME

SERIES = UP
REGIME_SYMBOL = "SPY"
SCHEMA_VERSION = 1


class MissingInput(RuntimeError):
    """The basket or regime label for the date is missing or unreadable."""


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
    basket_file: str
    regime_file: str
    schema_version: int = SCHEMA_VERSION

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


def latest_basket(as_of: date, basket_dir: str | Path | None = None) -> tuple[Path, TickerBasket]:
    """The newest SCAN-Upward Trend Momentum basket for as_of."""
    folder = Path(basket_dir) if basket_dir is not None else default_basket_dir()
    found = [
        (path, basket)
        for path, basket in _load_all(folder, as_of, load_basket, "basket")
        if basket.scan_name == SCAN_NAME and basket.as_of == as_of
    ]
    if not found:
        raise MissingInput(f"no {SCAN_NAME} basket for {as_of} in {folder}")
    return max(found, key=lambda item: item[1].created_at)


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
    basket: TickerBasket,
    regime: RegimeLabel,
    basket_file: str | Path,
    regime_file: str | Path,
    created_at: datetime | None = None,
) -> RouteDecision:
    """Decide whether UP playbooks may run on the basket's passed tickers."""
    if basket.as_of != regime.as_of:
        raise ValueError(f"basket is for {basket.as_of} but the regime label is for {regime.as_of}")
    passed = basket.tickers
    up_allowed = regime.label == UP
    if up_allowed:
        reason = f"{regime.symbol} regime is UP: UP playbooks are allowed for the passed tickers"
    elif regime.label == SIDE:
        reason = f"{regime.symbol} regime is SIDE: UP playbooks are blocked; passed tickers are not tradeable under UP"
    elif regime.label == DOWN:
        reason = f"{regime.symbol} regime is DOWN: UP playbooks are blocked; passed tickers are not tradeable under UP"
    else:
        raise ValueError(f"unknown regime label: {regime.label!r}")

    return RouteDecision(
        as_of=basket.as_of,
        created_at=created_at or datetime.now(UTC),
        series=SERIES,
        regime_symbol=regime.symbol,
        regime_label=regime.label,
        up_allowed=up_allowed,
        tradeable=passed if up_allowed else [],
        blocked=[] if up_allowed else passed,
        reason=reason,
        basket_file=str(basket_file),
        regime_file=str(regime_file),
    )


def route_for_date(
    as_of: date, basket_dir: str | Path | None = None, regime_dir: str | Path | None = None
) -> RouteDecision:
    """Load the latest basket and SPY label for as_of and route them."""
    basket_path, basket = latest_basket(as_of, basket_dir)
    regime_path, regime = latest_regime(as_of, regime_dir)
    return route(basket, regime, basket_path, regime_path)


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


def _load_all(folder: Path, as_of: date, loader, kind: str):
    """Load every file named for as_of. One that cannot be read stops the route: skipping
    it could quietly pick an older file than the one that was meant to count."""
    for path in sorted(folder.glob(f"*_{as_of}_*.json")):
        try:
            yield path, loader(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MissingInput(f"cannot read {kind} file {path}: {exc}") from exc
