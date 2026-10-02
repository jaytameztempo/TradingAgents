"""TickerBasket: what a SCAN found, saved as JSON so later steps can read it back.

A basket records the names that passed and, for every name that did not, why.
The audit trail has to explain a skipped ticker as well as a chosen one.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class BasketMember:
    ticker: str
    score: float
    metrics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Rejection:
    ticker: str
    reason: str


@dataclass(frozen=True)
class TickerBasket:
    scan_name: str
    as_of: date
    created_at: datetime
    universe: list[str]
    parameters: dict
    members: list[BasketMember]
    rejected: list[Rejection]
    schema_version: int = SCHEMA_VERSION

    @property
    def tickers(self) -> list[str]:
        return [m.ticker for m in self.members]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> TickerBasket:
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported basket schema_version: {data.get('schema_version')!r}")
        return cls(
            scan_name=data["scan_name"],
            as_of=date.fromisoformat(data["as_of"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            universe=list(data["universe"]),
            parameters=dict(data["parameters"]),
            members=[BasketMember(**m) for m in data["members"]],
            rejected=[Rejection(**r) for r in data["rejected"]],
        )


def default_basket_dir() -> Path:
    """~/.tradingagents/baskets, beside TradingAgents' own results and out of git."""
    return Path.home() / ".tradingagents" / "baskets"


def basket_filename(basket: TickerBasket) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", basket.scan_name.lower()).strip("-")
    return f"{slug}_{basket.as_of}_{basket.created_at:%Y%m%dT%H%M%SZ}.json"


def save_basket(basket: TickerBasket, out_dir: str | Path | None = None) -> Path:
    """Write the basket as a new file and return its path. An existing basket is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_basket_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / basket_filename(basket)
    with path.open("x", encoding="utf-8") as fh:
        json.dump(basket.to_dict(), fh, indent=2)
        fh.write("\n")
    return path


def load_basket(path: str | Path) -> TickerBasket:
    return TickerBasket.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
