"""SCANBot build step 2: the financial gates, strong and weak, as separate passes.

Both passes start from the universe report's liquidity survivors and run the
same gates in order, each with a count:

1. fundamentals_data: a fundamentals file no older than 10 days, with us-gaap facts
2. period_recency: the latest reported period ended within 15 months of as_of
3. diluted_eps, 4. operating_income, 5. operating_cash_flow: the trailing-twelve-
   month figure is above 0 (STRONG) or below 0 (WEAK). A missing figure, or one
   whose period ended more than 15 months before as_of, fails closed.

Only facts filed on or before as_of count. TTM is the latest annual figure when
nothing later has been filed; otherwise annual + this year to date - the same
span a year earlier. The method is recorded per figure. Diluted EPS summed that
way is an approximation when the share count moved.

The quality checks of section 3.2 are recorded for every survivor and required
by none: this step does not apply the two-of-five rule. Sector-median margin and
going-concern language are not in company facts and are recorded as None. The
turnaround rule is not applied. Nothing here places an order.
"""

from __future__ import annotations

import calendar
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from extensions.scanbot import fundamentals as fx
from extensions.scanbot.funnel import FunnelReport, StageCount, _Counter, default_report_dir
from extensions.scanbot.universe import Removal
from tradingagents.dataflows.vendors.sec_edgar import _ANNUAL_FORMS

REPORT_VERSION = 1
STRONG, WEAK = "STRONG", "WEAK"
MAX_PERIOD_AGE_MONTHS = 15
ANNUAL_SPAN = (300, 400)
YEAR_TO_DATE_SPAN = (60, 290)
QUARTER_SPAN = (60, 115)
ALIGN_DAYS = 10  # 52/53-week years move period edges by a few days
SHARES_LOOKBACK_TOLERANCE_DAYS = 45

REQUIRED = (
    ("diluted_eps", "diluted EPS"),
    ("operating_income", "operating income"),
    ("operating_cash_flow", "operating cash flow"),
)


def _d(text: str) -> date:
    return date.fromisoformat(text)


def _span(key: tuple[str, str]) -> int:
    return (_d(key[1]) - _d(key[0])).days


def months_before(day: date, months: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


@dataclass(frozen=True)
class TTM:
    value: float
    period_end: str
    method: str  # "annual" or "annual+ytd-prior_ytd"
    tag: str
    unit: str
    periods: dict[str, list[str]]  # component name -> [start, end]

    def to_dict(self) -> dict:
        return asdict(self)


def _duration_periods(facts: list[dict], as_of: str) -> dict[tuple[str, str], tuple[float, bool]]:
    """{(start, end): (value, filed on an annual form)} at the latest filing on or before as_of."""
    latest: dict[tuple[str, str], dict] = {}
    annual: set[tuple[str, str]] = set()
    for fact in facts:
        if "start" not in fact or fact["filed"] > as_of:
            continue
        key = (fact["start"], fact["end"])
        if fact.get("form", "").startswith(_ANNUAL_FORMS):
            annual.add(key)
        seen = latest.get(key)
        if seen is None or fact["filed"] >= seen["filed"]:
            latest[key] = fact
    return {key: (fact["val"], key in annual) for key, fact in latest.items()}


def _ttm_from_periods(periods: dict, tag: str, unit: str) -> TTM | None:
    annuals = [k for k, (_, is_annual) in periods.items() if is_annual and ANNUAL_SPAN[0] <= _span(k) <= ANNUAL_SPAN[1]]
    if not annuals:
        return None
    a = max(annuals, key=lambda k: k[1])
    a_start, a_end = _d(a[0]), _d(a[1])
    fiscal_start = a_end + timedelta(days=1)
    ytds = [
        k for k in periods
        if _d(k[1]) > a_end
        and YEAR_TO_DATE_SPAN[0] <= _span(k) <= YEAR_TO_DATE_SPAN[1]
        and abs((_d(k[0]) - fiscal_start).days) <= ALIGN_DAYS
    ]
    if not ytds:
        return TTM(periods[a][0], a[1], "annual", tag, unit, {"annual": list(a)})
    y = max(ytds, key=lambda k: k[1])
    prior_end = _d(y[1]) - timedelta(days=365)
    priors = [
        k for k in periods
        if abs((_d(k[0]) - a_start).days) <= ALIGN_DAYS
        and abs((_d(k[1]) - prior_end).days) <= ALIGN_DAYS
        and abs(_span(k) - _span(y)) <= ALIGN_DAYS
    ]
    if not priors:
        # The year to date cannot be annualised without its comparable, so the
        # latest full year stands; the recency gates judge its age.
        return TTM(periods[a][0], a[1], "annual", tag, unit, {"annual": list(a)})
    p = min(priors, key=lambda k: abs((_d(k[1]) - prior_end).days))
    value = periods[a][0] + periods[y][0] - periods[p][0]
    return TTM(value, y[1], "annual+ytd-prior_ytd", tag, unit,
               {"annual": list(a), "ytd": list(y), "prior_ytd": list(p)})


def ttm(ff: fx.FundamentalsFile, tags: tuple[str, ...], as_of: str) -> TTM | None:
    """The TTM figure from whichever tag reports the latest period; ties go to the earlier tag."""
    best: TTM | None = None
    for tag in tags:
        for unit, facts in ff.tag("us-gaap", tag).items():
            found = _ttm_from_periods(_duration_periods(facts, as_of), tag, unit)
            if found and (best is None or found.period_end > best.period_end):
                best = found
    return best


def _instants(ff: fx.FundamentalsFile, taxonomy: str, tags: tuple[str, ...], as_of: str) -> dict[str, float]:
    """{end: value} for instant facts filed by as_of, latest filing per end, first tag per end."""
    values: dict[str, float] = {}
    for tag in tags:
        latest: dict[str, dict] = {}
        for facts in ff.tag(taxonomy, tag).values():
            for fact in facts:
                if "start" in fact or fact["filed"] > as_of or fact["end"] > as_of:
                    continue
                seen = latest.get(fact["end"])
                if seen is None or fact["filed"] >= seen["filed"]:
                    latest[fact["end"]] = fact
        for end, fact in latest.items():
            values.setdefault(end, fact["val"])
    return values


def _quarters(ff: fx.FundamentalsFile, tags: tuple[str, ...], as_of: str) -> dict[str, float]:
    """{end: value} for discrete-quarter facts filed by as_of, first tag per end."""
    values: dict[str, float] = {}
    for tag in tags:
        for facts in ff.tag("us-gaap", tag).values():
            for key, (val, _) in _duration_periods(facts, as_of).items():
                if QUARTER_SPAN[0] <= _span(key) <= QUARTER_SPAN[1]:
                    values.setdefault(key[1], val)
    return values


def _ratio(numerator, denominator) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


@dataclass(frozen=True)
class Measurement:
    required: dict[str, TTM | None]
    latest_period_end: str | None
    extras: dict

    def to_dict(self) -> dict:
        return {
            "ttm": {name: (t.to_dict() if t else None) for name, t in self.required.items()},
            "latest_period_end": self.latest_period_end,
            **self.extras,
        }


def measure(ff: fx.FundamentalsFile, as_of: date) -> Measurement:
    asof = as_of.isoformat()
    required = {
        "diluted_eps": ttm(ff, fx.EPS_TAGS, asof),
        "operating_income": ttm(ff, fx.OPERATING_INCOME_TAGS, asof),
        "operating_cash_flow": ttm(ff, fx.OPERATING_CASH_FLOW_TAGS, asof),
    }
    ends = [t.period_end for t in required.values() if t]
    ocf, opinc = required["operating_cash_flow"], required["operating_income"]

    capex = ttm(ff, fx.CAPEX_TAGS, asof)
    fcf = ocf.value - capex.value if ocf and capex and capex.period_end == ocf.period_end else None

    interest = ttm(ff, fx.INTEREST_TAGS, asof)
    coverage = (
        _ratio(opinc.value, interest.value)
        if opinc and interest and interest.period_end == opinc.period_end else None
    )

    assets, liabilities = (_instants(ff, "us-gaap", tags, asof)
                           for tags in (fx.CURRENT_ASSETS_TAGS, fx.CURRENT_LIABILITIES_TAGS))
    common = sorted(set(assets) & set(liabilities))
    current_ratio = _ratio(assets[common[-1]], liabilities[common[-1]]) if common else None

    return Measurement(required, max(ends) if ends else None, {
        "free_cash_flow": fcf,
        "capex_ttm": capex.to_dict() if capex else None,
        "interest_expense_ttm": interest.to_dict() if interest else None,
        "interest_coverage": coverage,
        "current_ratio": current_ratio,
        "current_ratio_date": common[-1] if common else None,
        "gross_margins": _gross_margins(ff, asof),
        "shares_growth_yoy": _shares_growth(ff, asof),
    })


def _gross_margins(ff: fx.FundamentalsFile, as_of: str) -> list[list] | None:
    """[[end, margin], ...] for the last three consecutive filed quarters, or None."""
    revenue, gross = _quarters(ff, fx.REVENUE_TAGS, as_of), _quarters(ff, fx.GROSS_PROFIT_TAGS, as_of)
    ends = sorted(e for e in set(revenue) & set(gross) if revenue[e] > 0)[-3:]
    if len(ends) < 3 or any(not 80 <= (_d(b) - _d(a)).days <= 100 for a, b in zip(ends, ends[1:])):
        return None
    return [[e, gross[e] / revenue[e]] for e in ends]


def _shares_growth(ff: fx.FundamentalsFile, as_of: str) -> float | None:
    shares = _instants(ff, "dei", fx.SHARES_TAGS, as_of)
    if not shares:
        return None
    last = max(shares)
    target = _d(last) - timedelta(days=365)
    prior = [e for e in shares if abs((_d(e) - target).days) <= SHARES_LOOKBACK_TOLERANCE_DAYS]
    if not prior:
        return None
    base = shares[min(prior, key=lambda e: abs((_d(e) - target).days))]
    return shares[last] / base - 1 if base > 0 else None


def _compare(value, test) -> bool | None:
    return None if value is None else bool(test(value))


def quality_checks(m: Measurement, gate: str) -> dict[str, bool | None]:
    """Section 3.2's extra checks: True, False, or None when the data is not available. Recorded only."""
    fcf, ratio, coverage = m.extras["free_cash_flow"], m.extras["current_ratio"], m.extras["interest_coverage"]
    opinc = m.required["operating_income"]
    if gate == STRONG:
        return {
            "free_cash_flow_positive": _compare(fcf, lambda v: v > 0),
            "operating_margin_above_sector_median": None,
            "current_ratio_at_least_1_2": _compare(ratio, lambda v: v >= 1.2),
            "interest_coverage_above_3": _compare(coverage, lambda v: v > 3),
            "no_going_concern_language": None,
        }
    if opinc and opinc.value < 0:
        weak_coverage = True
    else:
        weak_coverage = _compare(coverage, lambda v: v < 1)
    margins = m.extras["gross_margins"]
    falling = None if margins is None else margins[0][1] > margins[1][1] > margins[2][1]
    growth = m.extras["shares_growth_yoy"]
    parts = [None, falling, _compare(growth, lambda v: v > 0.10)]  # going concern is not available
    strain = True if any(parts) else (None if None in parts else False)
    return {
        "free_cash_flow_negative": _compare(fcf, lambda v: v < 0),
        "operating_margin_below_sector_median": None,
        "current_ratio_below_1_0": _compare(ratio, lambda v: v < 1.0),
        "interest_coverage_below_1_or_negative_operating_income": weak_coverage,
        "going_concern_or_falling_gross_margin_or_dilution": strain,
        "detail_gross_margin_falling_two_quarters": falling,
        "detail_shares_up_over_10pct_yoy": parts[2],
    }


@dataclass(frozen=True)
class GatePass:
    financial_gate: str
    stage_counts: list[StageCount]
    survivors: list[str]
    survivor_metrics: dict[str, dict]
    removed: list[Removal]


@dataclass(frozen=True)
class FinancialsReport:
    as_of: date
    created_at: datetime
    universe_report: str
    universe_created_at: str
    universe_survivors: int
    fundamentals_dir: str
    policy: dict
    data_sources: dict[str, int]
    data_freshness: dict[str, str]
    passes: list[GatePass]
    warnings: list[str] = field(default_factory=list)
    report_version: int = REPORT_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> FinancialsReport:
        if data.get("report_version") != REPORT_VERSION:
            raise ValueError(f"unsupported financials report_version: {data.get('report_version')!r}")
        fields = dict(data)
        fields["as_of"] = date.fromisoformat(data["as_of"])
        fields["created_at"] = datetime.fromisoformat(data["created_at"])
        fields["passes"] = [
            GatePass(
                financial_gate=p["financial_gate"],
                stage_counts=[StageCount(**s) for s in p["stage_counts"]],
                survivors=p["survivors"],
                survivor_metrics=p["survivor_metrics"],
                removed=[Removal(**r) for r in p["removed"]],
            )
            for p in data["passes"]
        ]
        return cls(**fields)

    def gate_pass(self, gate: str) -> GatePass:
        return next(p for p in self.passes if p.financial_gate == gate)


def _data_reason(lookup: fx.Lookup | None) -> str | None:
    if lookup is None:
        return "no fundamentals lookup for this symbol"
    if lookup.file is None:
        return lookup.failure
    if lookup.file.status == fx.NO_FACTS:
        return f"SEC has no company facts for CIK {lookup.file.cik}"
    if "us-gaap" not in lookup.file.taxonomies:
        taxonomies = ", ".join(lookup.file.taxonomies) or "none"
        return f"no us-gaap facts (taxonomies: {taxonomies})"
    return None


def _run_pass(gate: str, symbols: list[str], lookups: dict[str, fx.Lookup],
              measurements: dict[str, Measurement], as_of: date) -> GatePass:
    counter = _Counter()
    cutoff = months_before(as_of, MAX_PERIOD_AGE_MONTHS).isoformat()

    def by_test(name: str, test):
        def run(items):
            kept, removed = [], []
            for symbol in items:
                reason = test(symbol)
                (kept.append(symbol) if reason is None else removed.append(Removal(symbol, name, reason)))
            return kept, removed
        return run

    def recency(symbol: str) -> str | None:
        end = measurements[symbol].latest_period_end
        if end is None:
            return f"no TTM diluted EPS, operating income or operating cash flow filed by {as_of}"
        if end < cutoff:
            return f"latest reported period ended {end}, more than {MAX_PERIOD_AGE_MONTHS} months before {as_of}"
        return None

    def metric(name: str, label: str):
        def test(symbol: str) -> str | None:
            t = measurements[symbol].required[name]
            if t is None:
                return f"{label} not reported in us-gaap facts filed by {as_of}"
            if t.period_end < cutoff:
                return f"{label} TTM period ended {t.period_end}, more than {MAX_PERIOD_AGE_MONTHS} months before {as_of}"
            if gate == STRONG and not t.value > 0:
                return f"{label} TTM {t.value:g} is not above 0"
            if gate == WEAK and not t.value < 0:
                return f"{label} TTM {t.value:g} is not below 0"
            return None
        return test

    kept = counter.apply("financials", "fundamentals_data", symbols, by_test("fundamentals_data",
                                                                             lambda s: _data_reason(lookups.get(s))))
    kept = counter.apply("financials", "period_recency", kept, by_test("period_recency", recency))
    for name, label in REQUIRED:
        kept = counter.apply("financials", name, kept, by_test(name, metric(name, label)))

    survivor_metrics = {}
    for symbol in kept:
        m = measurements[symbol]
        checks = quality_checks(m, gate)
        scored = {k: v for k, v in checks.items() if not k.startswith("detail_")}
        lookup = lookups[symbol]
        survivor_metrics[symbol] = {
            "cik": lookup.file.cik,
            "entity_name": lookup.file.entity_name,
            "fundamentals_fetched_at": lookup.file.fetched_at.isoformat(),
            **m.to_dict(),
            "quality_checks": checks,
            "quality_passed": sum(1 for v in scored.values() if v is True),
            "quality_available": sum(1 for v in scored.values() if v is not None),
        }
    return GatePass(gate, counter.counts, kept, survivor_metrics, counter.removed)


def run_financials(
    universe: FunnelReport,
    universe_path: str | Path,
    lookups: dict[str, fx.Lookup],
    *,
    fundamentals_dir: str | Path | None = None,
    created_at: datetime | None = None,
) -> FinancialsReport:
    """Run the STRONG and WEAK passes over the universe report's survivors."""
    as_of = universe.as_of
    symbols = list(universe.survivors)
    measurements = {
        s: measure(lookups[s].file, as_of)
        for s in symbols
        if _data_reason(lookups.get(s)) is None
    }
    passes = [_run_pass(gate, symbols, lookups, measurements, as_of) for gate in (STRONG, WEAK)]
    both = set(passes[0].survivors) & set(passes[1].survivors)
    if both:
        raise AssertionError(f"symbols passed both the strong and weak gates: {sorted(both)}")

    sources: dict[str, int] = {fx.CACHE: 0, fx.FETCHED: 0, fx.STALE_FALLBACK: 0, "failed": 0}
    warnings = []
    for s in symbols:
        lookup = lookups.get(s)
        sources[lookup.source if lookup and lookup.file else "failed"] += 1
        warnings.extend(f"{s}: {w}" for w in (lookup.warnings if lookup else []))
    freshness = {s: lookups[s].file.fetched_at.isoformat() for s in symbols if s in lookups and lookups[s].file}
    if not universe.point_in_time:
        warnings.append("the universe report is not point in time (its asset list was fetched after as_of)")

    return FinancialsReport(
        as_of=as_of,
        created_at=created_at or datetime.now(UTC),
        universe_report=str(universe_path),
        universe_created_at=universe.created_at.isoformat(),
        universe_survivors=len(symbols),
        fundamentals_dir=str(fundamentals_dir or fx.default_fundamentals_dir()),
        policy={
            "source": "SEC EDGAR company facts (us-gaap), facts filed on or before as_of",
            "reuse_days": fx.REUSE_DAYS,
            "max_age_days": fx.MAX_AGE_DAYS,
            "max_period_age_months": MAX_PERIOD_AGE_MONTHS,
            "ttm_methods": ["annual", "annual+ytd-prior_ytd"],
            "quality_checks_required": False,
            "turnaround_rule_applied": False,
        },
        data_sources=sources,
        data_freshness=freshness,
        passes=passes,
        warnings=warnings,
    )


def save_report(report: FinancialsReport, out_dir: str | Path | None = None) -> Path:
    """Write the report as a new file and return its path. An existing report is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_report_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"financials_{report.as_of}_{report.created_at:%Y%m%dT%H%M%SZ}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2)
        fh.write("\n")
    return path


def load_report(path: str | Path) -> FinancialsReport:
    return FinancialsReport.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
