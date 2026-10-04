"""Route both scan baskets by a market regime built from the SPY and QQQ labels for the same date.

The market label combines the saved RegimeBot labels; it never recomputes them:

- both UP: UP. UPBot is allowed for the upward-scan passers; DOWNBot is blocked.
- both DOWN: DOWN. DOWNBot is allowed for the breakdown-scan passers; UPBot is blocked.
- anything else (one label missing, the two disagree, or either is SIDE): SIDE.
  Both trend bots are blocked; passers from either scan are routed to SIDEBot.

The playbooks read ``regime_label`` (the market label), ``tradeable`` and
``blocked``: under UP or DOWN only the allowed bot's names are listed, in
``tradeable``; under SIDE every passer is listed in ``blocked``. Each scan's
passers and each symbol's label and file are also recorded, so the file
explains a blocked side and a SIDE market as well as an allowed one.

The router reads the latest label for each symbol and the latest basket of each
scan for the date, by the ``created_at`` recorded inside each file. If both
labels are missing there is no regime evidence at all, so it raises and no
decision is made. The basket for the allowed side is required; under SIDE
neither is. An unreadable candidate file also raises. It fetches no bars and
places no orders.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from extensions.regime.regime_bot import (
    DOWN,
    LABELS,
    SIDE,
    UP,
    RegimeLabel,
    default_regime_dir,
    load_label,
)
from extensions.scans.basket import TickerBasket, default_basket_dir, load_basket
from extensions.scans.breakdown_short_candidates import SCAN_NAME as DOWN_SCAN_NAME
from extensions.scans.upward_trend_momentum import SCAN_NAME as UP_SCAN_NAME

SERIES = "ALL"
REGIME_SYMBOL = "SPY"
REGIME_SYMBOLS = ("SPY", "QQQ")
# Fields added since version 1 are optional with defaults, so routes written
# before them still load and every version-1 reader still reads new routes.
SCHEMA_VERSION = 1

FoundLabel = tuple[str | Path, RegimeLabel]
FoundBasket = tuple[str | Path, TickerBasket]


class MissingInput(RuntimeError):
    """No regime label, a required basket, or a readable file for the date."""


@dataclass(frozen=True)
class RouteDecision:
    as_of: date
    created_at: datetime
    series: str
    regime_symbol: str  # the symbols behind the market label, e.g. "SPY+QQQ"
    regime_label: str  # the market label
    up_allowed: bool
    tradeable: list[str]
    blocked: list[str]
    reason: str
    basket_file: str | None  # the upward-scan basket
    regime_file: str | None  # the SPY label
    schema_version: int = SCHEMA_VERSION
    down_allowed: bool = False
    up_passed: list[str] = field(default_factory=list)
    down_passed: list[str] = field(default_factory=list)
    down_basket_file: str | None = None
    regime_labels: dict[str, str | None] = field(default_factory=dict)
    regime_files: dict[str, str | None] = field(default_factory=dict)

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
    found = _newest_label(as_of, folder, symbol)
    if found is None:
        raise MissingInput(f"no {symbol} regime label for {as_of} in {folder}")
    return found


def market_label(labels: dict[str, str | None]) -> tuple[str, str]:
    """Combine per-symbol labels into (market label, why). At least one label must be present."""
    unknown = [label for label in labels.values() if label is not None and label not in LABELS]
    if unknown:
        raise ValueError(f"unknown regime label: {unknown[0]!r}")
    present = {symbol: label for symbol, label in labels.items() if label is not None}
    if not present:
        raise ValueError("no regime label to combine")

    names = " and ".join(labels)
    missing = [symbol for symbol in labels if symbol not in present]
    if missing:
        return SIDE, f"{' and '.join(missing)} label is missing"
    values = set(present.values())
    if values == {UP}:
        return UP, f"{names} are both UP"
    if values == {DOWN}:
        return DOWN, f"{names} are both DOWN"
    sides = [symbol for symbol, label in present.items() if label == SIDE]
    if sides:
        return SIDE, f"{' and '.join(sides)} {'is' if len(sides) == 1 else 'are'} SIDE"
    return SIDE, f"{' but '.join(f'{s} is {label}' for s, label in present.items())}"


def route(
    as_of: date,
    labels: dict[str, FoundLabel | None],
    up: FoundBasket | None = None,
    down: FoundBasket | None = None,
    created_at: datetime | None = None,
) -> RouteDecision:
    """Decide which trend bot, if any, may run, and on which names.

    ``labels`` maps each regime symbol to its (file, label), or None when that
    symbol has no label for the date. ``up`` and ``down`` are (file, basket)
    for the upward and breakdown scans, or None when that scan has no basket.
    """
    for symbol, found in labels.items():
        if found is None:
            continue
        if found[1].symbol != symbol:
            raise ValueError(f"the {symbol} label file is for {found[1].symbol}")
        if found[1].as_of != as_of:
            raise ValueError(f"the route is for {as_of} but the {symbol} regime label is for {found[1].as_of}")
    for scan_name, found in ((UP_SCAN_NAME, up), (DOWN_SCAN_NAME, down)):
        if found is not None and found[1].as_of != as_of:
            raise ValueError(f"{scan_name} basket is for {found[1].as_of} but the route is for {as_of}")
    if all(found is None for found in labels.values()):
        raise MissingInput(f"no regime label for {as_of} for any of {', '.join(labels)}")

    label, why = market_label({s: found[1].label if found else None for s, found in labels.items()})
    market = "+".join(labels)
    up_passed = up[1].tickers if up is not None else []
    down_passed = down[1].tickers if down is not None else []

    if label == UP:
        _require(up, UP_SCAN_NAME, as_of, market, label)
        tradeable, blocked = up_passed, []
        action = "UPBot is allowed for the upward-scan passers; DOWNBot is blocked"
    elif label == DOWN:
        _require(down, DOWN_SCAN_NAME, as_of, market, label)
        tradeable, blocked = down_passed, []
        action = "DOWNBot is allowed for the breakdown-scan passers; UPBot is blocked"
    else:
        tradeable, blocked = [], list(dict.fromkeys(up_passed + down_passed))
        action = "UPBot and DOWNBot are blocked; passed names are routed to SIDEBot only"

    files = {s: str(found[0]) if found else None for s, found in labels.items()}
    return RouteDecision(
        as_of=as_of,
        created_at=created_at or datetime.now(UTC),
        series=SERIES,
        regime_symbol=market,
        regime_label=label,
        up_allowed=label == UP,
        down_allowed=label == DOWN,
        tradeable=tradeable,
        blocked=blocked,
        up_passed=up_passed,
        down_passed=down_passed,
        reason=f"{market} market is {label}: {why}; {action}",
        basket_file=str(up[0]) if up is not None else None,
        down_basket_file=str(down[0]) if down is not None else None,
        regime_file=files.get(REGIME_SYMBOL),
        regime_labels={s: found[1].label if found else None for s, found in labels.items()},
        regime_files=files,
    )


def route_for_date(
    as_of: date, basket_dir: str | Path | None = None, regime_dir: str | Path | None = None
) -> RouteDecision:
    """Load each symbol's latest label and each scan's latest basket for as_of, and route them."""
    regimes = Path(regime_dir) if regime_dir is not None else default_regime_dir()
    baskets = Path(basket_dir) if basket_dir is not None else default_basket_dir()
    labels = {symbol: _newest_label(as_of, regimes, symbol) for symbol in REGIME_SYMBOLS}
    up = _newest_basket(as_of, baskets, UP_SCAN_NAME)
    down = _newest_basket(as_of, baskets, DOWN_SCAN_NAME)
    return route(as_of, labels, up, down)


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


def _require(found, scan_name: str, as_of: date, market: str, label: str) -> None:
    if found is None:
        raise MissingInput(
            f"no {scan_name} basket for {as_of}; it is required when the {market} market is {label}"
        )


def _newest_label(as_of: date, folder: Path, symbol: str) -> tuple[Path, RegimeLabel] | None:
    """The newest label for symbol and as_of, or None if RegimeBot wrote none."""
    found = [
        (path, label)
        for path, label in _load_all(folder, as_of, load_label, "regime label")
        if label.symbol == symbol and label.as_of == as_of
    ]
    return max(found, key=lambda item: item[1].created_at) if found else None


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
