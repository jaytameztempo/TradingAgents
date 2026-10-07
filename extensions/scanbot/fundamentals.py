"""SCANBot build step 2 data: SEC EDGAR company facts, one cached file per symbol.

EDGAR's company-facts API is free and needs no key, only a User-Agent naming the
caller (see ``sec_edgar._user_agent``). Each symbol's file under
~/.tradingagents/fundamentals/ keeps only the tags the financial gates read, with
every fact's filed date, so a gate dated ``as_of`` can use exactly what was on
file then.

Freshness is the ``fetched_at`` written inside the file, not the file's modified
time, which a sync client can touch:

- under 7 days: used as is, never refetched
- 7 to 10 days: refetched; if the refetch fails, the old file is still used
- over 10 days, or no file, and the fetch fails: the symbol fails closed

A ticker map that cannot be loaded stops the run; nothing is written. Nothing
here places an order.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.vendors import sec_edgar

CACHE_VERSION = 1
REUSE_DAYS = 7
MAX_AGE_DAYS = 10
REQUESTS_PER_SECOND = 8  # SEC's limit is 10
DOWNLOAD_DEADLINE_SECONDS = 120
FACTS_URL = sec_edgar._FACTS_URL

# Tag groups, best tag first. The gates read these and nothing else is cached.
EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted")
OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
OPERATING_CASH_FLOW_TAGS = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
CAPEX_TAGS = ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")
INTEREST_TAGS = ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt")
REVENUE_TAGS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet")
GROSS_PROFIT_TAGS = ("GrossProfit",)
CURRENT_ASSETS_TAGS = ("AssetsCurrent",)
CURRENT_LIABILITIES_TAGS = ("LiabilitiesCurrent",)
SHARES_TAGS = ("EntityCommonStockSharesOutstanding",)  # dei taxonomy

US_GAAP_TAGS = (
    *EPS_TAGS, *OPERATING_INCOME_TAGS, *OPERATING_CASH_FLOW_TAGS, *CAPEX_TAGS, *INTEREST_TAGS,
    *REVENUE_TAGS, *GROSS_PROFIT_TAGS, *CURRENT_ASSETS_TAGS, *CURRENT_LIABILITIES_TAGS,
)
DEI_TAGS = SHARES_TAGS
_FACT_FIELDS = ("start", "end", "val", "filed", "form")

OK, NO_FACTS = "ok", "no_facts"
CACHE, FETCHED, STALE_FALLBACK = "cache", "fetched", "stale_fallback"


class FactsUnavailable(RuntimeError):
    """EDGAR could not serve a company's facts now (network, throttling, server error)."""


class TickerMapUnavailable(RuntimeError):
    """SEC's ticker-to-CIK map could not be loaded, so no symbol can be fetched."""


@dataclass(frozen=True)
class FundamentalsFile:
    symbol: str
    cik: str
    entity_name: str
    fetched_at: datetime
    status: str  # OK, or NO_FACTS when EDGAR has no company facts for the CIK
    taxonomies: list[str]
    facts: dict[str, dict[str, dict[str, list[dict]]]]  # taxonomy -> tag -> unit -> facts
    source_url: str = ""
    cache_version: int = CACHE_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["fetched_at"] = self.fetched_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> FundamentalsFile:
        if data.get("cache_version") != CACHE_VERSION:
            raise ValueError(f"unsupported fundamentals cache_version: {data.get('cache_version')!r}")
        fields = dict(data)
        fields["fetched_at"] = datetime.fromisoformat(data["fetched_at"])
        return cls(**fields)

    def tag(self, taxonomy: str, tag: str) -> dict[str, list[dict]]:
        return (self.facts.get(taxonomy) or {}).get(tag) or {}


@dataclass(frozen=True)
class Lookup:
    """One symbol's fundamentals file, or why there is none."""

    symbol: str
    file: FundamentalsFile | None
    source: str | None = None  # CACHE, FETCHED or STALE_FALLBACK when file is set
    failure: str | None = None
    warnings: list[str] = field(default_factory=list)


class Throttle:
    """Spaces requests at least 1/rate seconds apart."""

    def __init__(self, rate: float = REQUESTS_PER_SECOND, clock=time.monotonic, sleep=time.sleep):
        self.interval = 1.0 / rate
        self.clock, self.sleep = clock, sleep
        self.last: float | None = None

    def wait(self) -> None:
        if self.last is not None:
            remaining = self.interval - (self.clock() - self.last)
            if remaining > 0:
                self.sleep(remaining)
        self.last = self.clock()


def default_fundamentals_dir() -> Path:
    """~/.tradingagents/fundamentals, beside the scanbot reports and out of git."""
    return Path.home() / ".tradingagents" / "fundamentals"


def sec_symbol(symbol: str) -> str:
    """SEC writes share classes with a dash: Alpaca's BRK.B is SEC's BRK-B."""
    return symbol.strip().upper().replace(".", "-")


def load_ticker_map() -> dict[str, tuple[str, str]]:
    """{SEC ticker: (10-digit CIK, company title)}, from the vendor's daily cache."""
    try:
        table = sec_edgar._cached_json(sec_edgar._TICKERS_URL, "company_tickers.json")
    except VendorUnavailableError as exc:
        raise TickerMapUnavailable(str(exc)) from exc
    return {
        entry["ticker"].upper(): (f"{int(entry['cik_str']):010d}", entry.get("title", ""))
        for entry in table.values()
        if entry.get("ticker")
    }


def fetch_company_facts(cik: str, deadline_seconds: float = DOWNLOAD_DEADLINE_SECONDS,
                        clock=time.monotonic) -> dict | None:
    """The raw company-facts document, or None when EDGAR has none for this CIK (404).

    requests' timeout bounds each read, not the download, so a response that
    trickles in can hang a run; the body is read in chunks against a deadline.
    """
    started = clock()
    try:
        response = requests.get(
            FACTS_URL.format(cik=cik), headers={"User-Agent": sec_edgar._user_agent()}, timeout=30, stream=True
        )
        with response:
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise FactsUnavailable(f"HTTP {response.status_code}")
            chunks = []
            for chunk in response.iter_content(chunk_size=1 << 16):
                chunks.append(chunk)
                if clock() - started > deadline_seconds:
                    raise FactsUnavailable(f"download exceeded {deadline_seconds:g}s")
    except requests.RequestException as exc:
        raise FactsUnavailable(type(exc).__name__) from exc
    try:
        return json.loads(b"".join(chunks))
    except ValueError as exc:
        raise FactsUnavailable("unreadable response") from exc


def extract(symbol: str, cik: str, raw: dict | None, fetched_at: datetime, entity_name: str = "") -> FundamentalsFile:
    """Keep only the cached tags, and only the fields a gate reads, from a company-facts document."""
    if raw is None:
        return FundamentalsFile(symbol, cik, entity_name, fetched_at, NO_FACTS, [], {}, FACTS_URL.format(cik=cik))
    all_facts = raw.get("facts") or {}
    kept: dict[str, dict] = {}
    for taxonomy, tags in (("us-gaap", US_GAAP_TAGS), ("dei", DEI_TAGS)):
        source = all_facts.get(taxonomy) or {}
        chosen = {}
        for tag in tags:
            units = (source.get(tag) or {}).get("units") or {}
            if units:
                chosen[tag] = {
                    unit: [{k: f[k] for k in _FACT_FIELDS if k in f} for f in facts]
                    for unit, facts in units.items()
                }
        if chosen:
            kept[taxonomy] = chosen
    return FundamentalsFile(
        symbol=symbol,
        cik=cik,
        entity_name=raw.get("entityName") or entity_name,
        fetched_at=fetched_at,
        status=OK,
        taxonomies=sorted(all_facts),
        facts=kept,
        source_url=FACTS_URL.format(cik=cik),
    )


_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def file_path(folder: str | Path, symbol: str) -> Path:
    """The symbol's file. Windows device names (CON is a listed ticker) take a trailing underscore:
    opening CON.json opens the console, and a read then waits for input."""
    stem = symbol.upper()
    return Path(folder) / f"{stem}_.json" if stem in _RESERVED_NAMES else Path(folder) / f"{stem}.json"


def read_file(path: str | Path) -> FundamentalsFile | None:
    """The cached file, or None when it is missing or unreadable (a miss, not a failure)."""
    try:
        return FundamentalsFile.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_file(folder: str | Path, ff: FundamentalsFile) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = file_path(folder, ff.symbol)
    replace_file(path, lambda temp: Path(temp).write_text(json.dumps(ff.to_dict()), encoding="utf-8"))
    return path


def _age_days(ff: FundamentalsFile, now: datetime) -> float:
    return (now - ff.fetched_at).total_seconds() / 86400


def load_fundamentals(
    symbols: list[str],
    *,
    folder: str | Path | None = None,
    now: datetime | None = None,
    offline: bool = False,
    ticker_map_loader: Callable[[], dict[str, tuple[str, str]]] = load_ticker_map,
    facts_fetcher: Callable[[str], dict | None] = fetch_company_facts,
    throttle: Throttle | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Lookup]:
    """A Lookup per symbol, applying the 7-day reuse and 10-day fail-closed rules."""
    folder = Path(folder) if folder is not None else default_fundamentals_dir()
    now = now or datetime.now(UTC)
    throttle = throttle or Throttle()
    reuse, max_age = timedelta(days=REUSE_DAYS), timedelta(days=MAX_AGE_DAYS)
    ticker_map: dict[str, tuple[str, str]] | None = None
    lookups: dict[str, Lookup] = {}

    for i, symbol in enumerate(symbols, 1):
        existing = read_file(file_path(folder, symbol))
        age = now - existing.fetched_at if existing else None

        if existing and age < reuse:
            lookups[symbol] = Lookup(symbol, existing, CACHE)
        elif offline:
            if existing and age <= max_age:
                lookups[symbol] = Lookup(symbol, existing, CACHE, warnings=[
                    f"file is {_age_days(existing, now):.1f} days old; not refetched (offline)"])
            else:
                lookups[symbol] = Lookup(symbol, None, failure=_no_file_reason("offline, no fetch", existing, now))
        else:
            if ticker_map is None:
                ticker_map = ticker_map_loader()
            lookups[symbol] = _fetch_one(symbol, existing, now, folder, ticker_map, facts_fetcher, throttle)

        if progress:
            progress(i, len(symbols))
    return lookups


def _fetch_one(symbol, existing, now, folder, ticker_map, facts_fetcher, throttle) -> Lookup:
    entry = ticker_map.get(sec_symbol(symbol))
    if entry is None:
        problem = f"{sec_symbol(symbol)} is not in SEC's ticker map"
    else:
        cik, title = entry
        throttle.wait()
        try:
            raw = facts_fetcher(cik)
        except FactsUnavailable as exc:
            problem = f"company facts fetch failed ({exc})"
        else:
            ff = extract(symbol, cik, raw, now, title)
            write_file(folder, ff)
            return Lookup(symbol, ff, FETCHED)

    if existing and now - existing.fetched_at <= timedelta(days=MAX_AGE_DAYS):
        return Lookup(symbol, existing, STALE_FALLBACK, warnings=[
            f"{problem}; used the file fetched {_age_days(existing, now):.1f} days ago"])
    return Lookup(symbol, None, failure=_no_file_reason(problem, existing, now))


def _no_file_reason(problem: str, existing: FundamentalsFile | None, now: datetime) -> str:
    if existing is None:
        return f"{problem}; no cached file"
    return f"{problem}; cached file is {_age_days(existing, now):.1f} days old, over {MAX_AGE_DAYS}"
