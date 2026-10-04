"""SCANBot stage 0: the listing universe, from Alpaca's asset records.

Each gate takes the records still in play and returns (kept, removed), so the
funnel can count exactly what each one drops:

1. class is us_equity
2. status is active
3. tradable is true
4. exchange is NYSE, NASDAQ or AMEX (OTC and everything else is dropped)
5. common stock only

Alpaca's asset record has no security-type field, so gate 5 works from the
name and symbol: ETFs, ETNs, funds, leveraged or inverse products, warrants,
rights, units, preferreds and listed notes are recognised by pattern. It is
approximate, and every removal names the pattern that matched. Shortable and
easy-to-borrow are not gates here; they matter only to short modes, later.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from extensions.market_data.alpaca_bars import validate_ticker

LISTED_EXCHANGES = ("NYSE", "NASDAQ", "AMEX")

# (label, pattern) pairs matched against the asset name, case-insensitive.
NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("ETF", r"\bETFs?\b"),
    ("ETN", r"\bETNs?\b"),
    ("exchange-traded product", r"\bexchange[- ]traded\b"),
    ("fund", r"\bfunds?\b"),
    ("unit trust", r"\btrust,?\s+series\b"),
    ("fund family", r"\b(?:ishares|spdr|proshares|direxion)\b"),
    ("leveraged or inverse", r"\b(?:leveraged|inverse|ultrashort|ultrapro)\b|\b\d(?:\.\d+)?x\b"),
    ("warrant", r"\bwarrants?\b"),
    ("right", r"\brights?\b"),
    ("unit", r"\bunits?\b"),
    ("preferred", r"\b(?:preferred|preference|pfd)\b"),
    ("note or debenture", r"\bnotes?\s+due\b|\bdebentures?\b|\bdue\s+20\d\d\b"),
    ("fixed coupon", r"\d(?:\.\d+)?\s*%"),
)
_NAME_RES = tuple((label, re.compile(pattern, re.IGNORECASE)) for label, pattern in NAME_PATTERNS)

# Share-class suffixes such as BRK.B pass; these suffixes mark other securities.
_SUFFIX = re.compile(r"[.-](?P<suffix>WS|W|UN|U|RT|R|P[A-Z]?)$")
_SUFFIX_LABELS = {"WS": "warrant", "W": "warrant", "UN": "unit", "U": "unit", "RT": "right", "R": "right"}
# Nasdaq's fifth letter W, R or U marks a warrant, right or unit.
_NASDAQ_FIFTH = re.compile(r"^[A-Z]{4}(?P<letter>[WRU])$")
_FIFTH_LABELS = {"W": "warrant", "R": "right", "U": "unit"}


@dataclass(frozen=True)
class Removal:
    symbol: str
    gate: str
    reason: str


GateResult = tuple[list, list[Removal]]


def _symbol(asset: dict) -> str:
    symbol = asset.get("symbol")
    return symbol.strip().upper() if isinstance(symbol, str) and symbol.strip() else "?"


def _field_gate(name: str, test: Callable[[dict], str | None]) -> Callable[[list[dict]], GateResult]:
    """A gate that removes every asset for which test returns a reason."""

    def gate(assets: list[dict]) -> GateResult:
        kept, removed = [], []
        for asset in assets:
            reason = test(asset)
            if reason is None:
                kept.append(asset)
            else:
                removed.append(Removal(_symbol(asset), name, reason))
        return kept, removed

    gate.__name__ = f"gate_{name}"
    return gate


def _is(field: str, expected) -> Callable[[dict], str | None]:
    def test(asset: dict) -> str | None:
        value = asset.get(field)
        return None if value == expected else f"{field} is {value!r}, needs {expected!r}"

    return test


def _exchange(asset: dict) -> str | None:
    exchange = asset.get("exchange")
    return None if exchange in LISTED_EXCHANGES else f"exchange is {exchange!r}, needs one of {', '.join(LISTED_EXCHANGES)}"


def non_common_reason(asset: dict) -> str | None:
    """Why this record does not look like common stock, or None if it does."""
    raw = asset.get("symbol")
    try:
        symbol = validate_ticker(raw if isinstance(raw, str) else "")
    except ValueError:
        return f"symbol {raw!r} is not a plain ticker"
    if match := _SUFFIX.search(symbol):
        suffix = match["suffix"]
        return f"symbol suffix {suffix} marks a {_SUFFIX_LABELS.get(suffix, 'preferred')}"
    if asset.get("exchange") == "NASDAQ" and (match := _NASDAQ_FIFTH.match(symbol)):
        return f"Nasdaq fifth letter {match['letter']} marks a {_FIFTH_LABELS[match['letter']]}"
    name = asset.get("name") or ""
    for label, pattern in _NAME_RES:
        if pattern.search(name):
            return f"name matches {label}: {name!r}"
    return None


def gate_common_stock(assets: list[dict]) -> GateResult:
    """Keep common stock, once per symbol."""
    kept, removed, seen = [], [], set()
    for asset in assets:
        reason = non_common_reason(asset)
        symbol = _symbol(asset)
        if reason is None and symbol in seen:
            reason = "duplicate listing of the symbol"
        if reason is None:
            seen.add(symbol)
            kept.append(asset)
        else:
            removed.append(Removal(symbol, "common_stock", reason))
    return kept, removed


gate_class = _field_gate("class", _is("class", "us_equity"))
gate_status = _field_gate("status", _is("status", "active"))
gate_tradable = _field_gate("tradable", _is("tradable", True))
gate_exchange = _field_gate("exchange", _exchange)

UNIVERSE_GATES: tuple[tuple[str, Callable[[list[dict]], GateResult]], ...] = (
    ("class", gate_class),
    ("status", gate_status),
    ("tradable", gate_tradable),
    ("exchange", gate_exchange),
    ("common_stock", gate_common_stock),
)


def universe_symbol(asset: dict) -> str:
    """The validated ticker of an asset that passed gate_common_stock."""
    return validate_ticker(asset["symbol"])
