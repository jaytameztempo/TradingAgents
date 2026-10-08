"""Rank and cap the saved SIDE channel baskets (SCANBot.md 7). It places no orders.

Reads only saved reports: the long and short channel reports for one as_of,
the financials report they both name, and the universe report that names.
Nothing is fetched and no scan is re-run.

LONG_SIDE, 100 points:
- range cleanliness 35: touches on the thinner side (3/7), low ADX (2/7),
  recorded quality checks passed out of 4 (2/7)
- low-pivot proximity 30: full at support, 0 at 0.6 ATR above or 0.25 ATR below
- volatility fit 15: full for ATR% 3-5, linear to 0 at 2 and at 8
- liquidity 10: 20-day dollar volume on a log scale $20M-$500M (7),
  median spread 0.05%-0.15% (3)
- strong-financials quality 10: free cash flow > 0 (4), interest coverage > 3 (3),
  current ratio >= 1.2 (3); margin vs sector has no input and scores 0

SHORT_SIDE, 100 points:
- range cleanliness 30: same build as the long side
- high-pivot proximity 30: full at resistance, 0 at 0.6 ATR below or 0.25 ATR through
- weakness of financials 20: the WEAK quality checks that are true
- liquidity and borrow 15: dollar volume (7), spread (3), shortable and easy to borrow (5)
- relative weakness 5: needs rs_63, which no saved report has

Every missing input scores 0. Nothing is invented.

Long names are walked best first. A name whose sector already holds
SECTOR_CAP names is dropped for the sector cap, and the walk stops at
MAX_NAMES. Short names are all kept, ranked, and never sector-capped. Sectors
come from the operator-supplied map in sector_map.py.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from extensions.scanbot.funnel import default_report_dir
from extensions.scanbot.sector_map import SECTOR_MAP_NOTE, SECTOR_SOURCE, UNKNOWN_SECTOR, sector_of

REPORT_VERSION = 1
LONG_SCAN = "SCAN-LongSidewaysChannel"
SHORT_SCAN = "SCAN-ShortSidewaysChannel"

MAX_NAMES = 15
SECTOR_CAP_PCT = 30.0
SECTOR_CAP = int(MAX_NAMES * SECTOR_CAP_PCT / 100)  # 4

LONG_WEIGHTS = {
    "range_cleanliness": 35.0,
    "pivot_proximity": 30.0,
    "volatility_fit": 15.0,
    "liquidity": 10.0,
    "financials": 10.0,
}
SHORT_WEIGHTS = {
    "range_cleanliness": 30.0,
    "pivot_proximity": 30.0,
    "financials": 20.0,
    "liquidity": 15.0,
    "relative_weakness": 5.0,
}

# Range cleanliness parts, as shares of the term's weight.
TOUCH_SHARE, ADX_SHARE, QUALITY_SHARE = 3 / 7, 2 / 7, 2 / 7
TOUCH_FLOOR, TOUCH_FULL = 1, 6
ADX_FULL, ADX_ZERO = 10.0, 20.0
QUALITY_CHECK_COUNT = 4

PIVOT_ATR_MAX = 0.6
BREAK_ATR_MAX = 0.25

ATR_ZERO_LOW, ATR_FULL_LOW, ATR_FULL_HIGH, ATR_ZERO_HIGH = 2.0, 3.0, 5.0, 8.0

DOLLAR_VOLUME_ZERO, DOLLAR_VOLUME_FULL = 20_000_000.0, 500_000_000.0
SPREAD_FULL_PCT, SPREAD_ZERO_PCT = 0.05, 0.15
DOLLAR_VOLUME_POINTS, SPREAD_POINTS, BORROW_POINTS = 7.0, 3.0, 5.0

STRONG_POINTS = {
    "free_cash_flow_positive": 4.0,
    "interest_coverage_above_3": 3.0,
    "current_ratio_at_least_1_2": 3.0,
    "operating_margin_above_sector_median": 0.0,
}
WEAK_POINTS = {
    "free_cash_flow_negative": 5.0,
    "current_ratio_below_1_0": 4.0,
    "interest_coverage_below_1_or_negative_operating_income": 4.0,
    "going_concern_or_falling_gross_margin_or_dilution": 4.0,
    "operating_margin_below_sector_median": 3.0,
}

EMITTED = "emitted"
DROPPED_SECTOR_CAP = "dropped_sector_cap"
DROPPED_MAX_NAMES = "dropped_max_names"
KEPT_SHORT = "kept"


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def range_cleanliness(call: dict, weight: float) -> float:
    metrics = call.get("metrics") or {}
    support, resistance = metrics.get("support_touches"), metrics.get("resistance_touches")
    touches = min(len(support), len(resistance)) if support is not None and resistance is not None else None
    touch = _clamp((touches - TOUCH_FLOOR) / (TOUCH_FULL - TOUCH_FLOOR)) if touches is not None else 0.0
    adx = _num(metrics.get("adx_14"))
    adx_part = _clamp((ADX_ZERO - adx) / (ADX_ZERO - ADX_FULL)) if adx is not None else 0.0
    passed = _num(call.get("quality_passed"))
    quality = _clamp(passed / QUALITY_CHECK_COUNT) if passed is not None else 0.0
    return weight * (TOUCH_SHARE * touch + ADX_SHARE * adx_part + QUALITY_SHARE * quality)


def pivot_proximity(distance_atr: float | None, weight: float, *, through_is_positive: bool) -> float:
    """Full at the line, 0 at PIVOT_ATR_MAX on the inside or BREAK_ATR_MAX through it.

    Long: distance_to_support_atr is positive above support (inside), so
    through_is_positive is False. Short: distance_to_resistance_atr is
    positive above resistance (through), so through_is_positive is True.
    """
    if distance_atr is None:
        return 0.0
    through = distance_atr > 0 if through_is_positive else distance_atr < 0
    limit = BREAK_ATR_MAX if through else PIVOT_ATR_MAX
    return weight * _clamp(1.0 - abs(distance_atr) / limit)


def volatility_fit(atr_pct: float | None, weight: float) -> float:
    if atr_pct is None:
        return 0.0
    if atr_pct < ATR_FULL_LOW:
        frac = (atr_pct - ATR_ZERO_LOW) / (ATR_FULL_LOW - ATR_ZERO_LOW)
    elif atr_pct > ATR_FULL_HIGH:
        frac = (ATR_ZERO_HIGH - atr_pct) / (ATR_ZERO_HIGH - ATR_FULL_HIGH)
    else:
        frac = 1.0
    return weight * _clamp(frac)


def liquidity_points(dollar_volume: float | None, spread_pct: float | None) -> float:
    dv = 0.0
    if dollar_volume is not None and dollar_volume > 0:
        dv = _clamp(math.log(dollar_volume / DOLLAR_VOLUME_ZERO) / math.log(DOLLAR_VOLUME_FULL / DOLLAR_VOLUME_ZERO))
    spread = 0.0
    if spread_pct is not None:
        spread = _clamp((SPREAD_ZERO_PCT - spread_pct) / (SPREAD_ZERO_PCT - SPREAD_FULL_PCT))
    return DOLLAR_VOLUME_POINTS * dv + SPREAD_POINTS * spread


def checks_points(checks: dict | None, points: dict[str, float]) -> float:
    checks = checks or {}
    return sum(p for name, p in points.items() if checks.get(name) is True)


def strong_financials_points(fin: dict | None) -> float:
    """Recomputed from the saved values so a None check still scores 0."""
    fin = fin or {}
    fcf = _num(fin.get("free_cash_flow"))
    cover = _num(fin.get("interest_coverage"))
    ratio = _num(fin.get("current_ratio"))
    checks = {
        "free_cash_flow_positive": fcf is not None and fcf > 0,
        "interest_coverage_above_3": cover is not None and cover > 3.0,
        "current_ratio_at_least_1_2": ratio is not None and ratio >= 1.2,
        "operating_margin_above_sector_median": (fin.get("quality_checks") or {}).get("operating_margin_above_sector_median"),
    }
    return checks_points(checks, STRONG_POINTS)


def weak_financials_points(fin: dict | None) -> float:
    return checks_points((fin or {}).get("quality_checks"), WEAK_POINTS)


def borrow_points(metrics: dict) -> float:
    return BORROW_POINTS if metrics.get("shortable") is True and metrics.get("easy_to_borrow") is True else 0.0


def _inputs(call: dict, fin: dict | None, liq: dict | None) -> dict:
    metrics = call.get("metrics") or {}
    fin, liq = fin or {}, liq or {}
    return {
        "close": metrics.get("close"),
        "adx_14": metrics.get("adx_14"),
        "atr_pct": metrics.get("atr_pct"),
        "height_pct": metrics.get("height_pct"),
        "support": metrics.get("support"),
        "resistance": metrics.get("resistance"),
        "support_touches": len(metrics.get("support_touches") or []),
        "resistance_touches": len(metrics.get("resistance_touches") or []),
        "distance_to_support_atr": metrics.get("distance_to_support_atr"),
        "distance_to_resistance_atr": metrics.get("distance_to_resistance_atr"),
        "quality_passed": call.get("quality_passed"),
        "avg_dollar_volume_20": liq.get("avg_dollar_volume_20"),
        "median_spread_pct": liq.get("median_spread_pct"),
        "free_cash_flow": fin.get("free_cash_flow"),
        "interest_coverage": fin.get("interest_coverage"),
        "current_ratio": fin.get("current_ratio"),
        "financial_checks": fin.get("quality_checks"),
        "shortable": metrics.get("shortable"),
        "easy_to_borrow": metrics.get("easy_to_borrow"),
        "rs_63": None,
    }


def score_long(call: dict, fin: dict | None, liq: dict | None) -> dict[str, float]:
    metrics = call.get("metrics") or {}
    liq = liq or {}
    w = LONG_WEIGHTS
    return {
        "range_cleanliness": range_cleanliness(call, w["range_cleanliness"]),
        "pivot_proximity": pivot_proximity(
            _num(metrics.get("distance_to_support_atr")), w["pivot_proximity"], through_is_positive=False
        ),
        "volatility_fit": volatility_fit(_num(metrics.get("atr_pct")), w["volatility_fit"]),
        "liquidity": liquidity_points(_num(liq.get("avg_dollar_volume_20")), _num(liq.get("median_spread_pct"))),
        "financials": strong_financials_points(fin),
    }


def score_short(call: dict, fin: dict | None, liq: dict | None) -> dict[str, float]:
    metrics = call.get("metrics") or {}
    liq = liq or {}
    w = SHORT_WEIGHTS
    return {
        "range_cleanliness": range_cleanliness(call, w["range_cleanliness"]),
        "pivot_proximity": pivot_proximity(
            _num(metrics.get("distance_to_resistance_atr")), w["pivot_proximity"], through_is_positive=True
        ),
        "financials": weak_financials_points(fin),
        "liquidity": liquidity_points(_num(liq.get("avg_dollar_volume_20")), _num(liq.get("median_spread_pct")))
        + borrow_points(metrics),
        "relative_weakness": 0.0,  # no rs_63 in any saved report
    }


def _ranked(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (-r["score"], r["symbol"]))


def apply_caps(rows: list[dict], *, max_names: int = MAX_NAMES, sector_cap: int = SECTOR_CAP) -> list[dict]:
    """Walk best first; set status and rank on each row. Rank counts every scored name."""
    taken: dict[str, int] = {}
    emitted = 0
    out = []
    for i, row in enumerate(_ranked(rows), start=1):
        row = {**row, "rank": i}
        if taken.get(row["sector"], 0) >= sector_cap:
            row["status"] = DROPPED_SECTOR_CAP
        elif emitted >= max_names:
            row["status"] = DROPPED_MAX_NAMES
        else:
            row["status"] = EMITTED
            taken[row["sector"]] = taken.get(row["sector"], 0) + 1
            emitted += 1
        out.append(row)
    return out


def _row(symbol: str, call: dict, fin: dict | None, liq: dict | None, parts: dict[str, float]) -> dict:
    return {
        "symbol": symbol,
        "sector": sector_of(symbol),
        "score": round(sum(parts.values()), 2),
        "components": {k: round(v, 2) for k, v in parts.items()},
        "inputs": _inputs(call, fin, liq),
    }


@dataclass(frozen=True)
class ChannelRankReport:
    as_of: date
    created_at: datetime
    sources: dict[str, str]
    policy: dict
    long_ranked: list[dict]
    short_ranked: list[dict]
    sector_counts: dict[str, int]
    warnings: list[str] = field(default_factory=list)
    report_version: int = REPORT_VERSION

    @property
    def long_emitted(self) -> list[str]:
        return [r["symbol"] for r in self.long_ranked if r["status"] == EMITTED]

    @property
    def short_kept(self) -> list[str]:
        return [r["symbol"] for r in self.short_ranked]

    def to_dict(self) -> dict:
        out = asdict(self)
        out["as_of"] = self.as_of.isoformat()
        out["created_at"] = self.created_at.isoformat()
        out["long_emitted"] = self.long_emitted
        out["short_kept"] = self.short_kept
        return out

    @classmethod
    def from_dict(cls, data: dict) -> ChannelRankReport:
        return cls(
            as_of=date.fromisoformat(data["as_of"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            sources=dict(data["sources"]),
            policy=dict(data["policy"]),
            long_ranked=list(data["long_ranked"]),
            short_ranked=list(data["short_ranked"]),
            sector_counts=dict(data["sector_counts"]),
            warnings=list(data.get("warnings", [])),
            report_version=int(data.get("report_version", REPORT_VERSION)),
        )


def _policy() -> dict:
    return {
        "long_weights": LONG_WEIGHTS,
        "short_weights": SHORT_WEIGHTS,
        "max_names": MAX_NAMES,
        "sector_cap_pct": SECTOR_CAP_PCT,
        "sector_cap_names": SECTOR_CAP,
        "sector_cap_applies_to": "long only",
        "short_names": "all kept and ranked, never dropped for sector or count",
        "sector_source": SECTOR_SOURCE,
        "sector_map": SECTOR_MAP_NOTE,
        "missing_input_scores": 0,
        "range_cleanliness_shares": {"touches": TOUCH_SHARE, "adx": ADX_SHARE, "quality_checks": QUALITY_SHARE},
        "touch_scale": [TOUCH_FLOOR, TOUCH_FULL],
        "adx_scale": [ADX_FULL, ADX_ZERO],
        "pivot_atr_inside_max": PIVOT_ATR_MAX,
        "pivot_atr_through_max": BREAK_ATR_MAX,
        "atr_pct_fit": [ATR_ZERO_LOW, ATR_FULL_LOW, ATR_FULL_HIGH, ATR_ZERO_HIGH],
        "dollar_volume_log_scale": [DOLLAR_VOLUME_ZERO, DOLLAR_VOLUME_FULL],
        "spread_pct_scale": [SPREAD_FULL_PCT, SPREAD_ZERO_PCT],
        "strong_financials_points": STRONG_POINTS,
        "weak_financials_points": WEAK_POINTS,
        "borrow_points": BORROW_POINTS,
        "orders": "none; this step only ranks",
    }


def _survivor_metrics(financials: dict, gate: str) -> dict:
    for p in financials.get("passes", []):
        if p.get("financial_gate") == gate:
            return p.get("survivor_metrics") or {}
    raise ValueError(f"financials report has no {gate} pass")


def rank_channels(
    long_report: dict,
    short_report: dict,
    financials: dict,
    universe: dict,
    *,
    sources: dict[str, str] | None = None,
    now: datetime | None = None,
) -> ChannelRankReport:
    if long_report.get("scan_name") != LONG_SCAN or short_report.get("scan_name") != SHORT_SCAN:
        raise ValueError("expected one SCAN-LongSidewaysChannel and one SCAN-ShortSidewaysChannel report")
    as_of = date.fromisoformat(long_report["as_of"])
    if date.fromisoformat(short_report["as_of"]) != as_of or date.fromisoformat(financials["as_of"]) != as_of:
        raise ValueError("long, short and financials reports must share one as_of")

    warnings: list[str] = []
    if long_report.get("financials_report") != short_report.get("financials_report"):
        warnings.append("long and short channel reports name different financials reports")
    for w in [*long_report.get("warnings", []), *short_report.get("warnings", [])]:
        if w not in warnings:
            warnings.append(w)

    strong, weak = _survivor_metrics(financials, "STRONG"), _survivor_metrics(financials, "WEAK")
    liquidity = universe.get("survivor_metrics") or {}

    long_rows = []
    for s in long_report["members"]:
        call = long_report["calls"][s]
        long_rows.append(_row(s, call, strong.get(s), liquidity.get(s), score_long(call, strong.get(s), liquidity.get(s))))
    short_rows = []
    for i, row in enumerate(
        _ranked(
            [
                _row(s, short_report["calls"][s], weak.get(s), liquidity.get(s),
                     score_short(short_report["calls"][s], weak.get(s), liquidity.get(s)))
                for s in short_report["members"]
            ]
        ),
        start=1,
    ):
        short_rows.append({**row, "rank": i, "status": KEPT_SHORT})

    for rows, metrics in ((long_rows, strong), (short_rows, weak)):
        for row in rows:
            s = row["symbol"]
            if row["sector"] == UNKNOWN_SECTOR:
                warnings.append(f"{s}: not in the operator sector map; ranked under {UNKNOWN_SECTOR}")
            if s not in liquidity:
                warnings.append(f"{s}: no universe liquidity metrics; liquidity scored 0")
            if s not in metrics:
                warnings.append(f"{s}: no financials metrics; financials scored 0")
    warnings.append("margin vs sector median is null in the financials report; it scores 0")
    if short_rows:
        warnings.append("relative weakness (rs_63) is in no saved report; it scores 0 on every short name")
    warnings.append(f"sectors are {SECTOR_SOURCE}: {SECTOR_MAP_NOTE}")

    ranked_long = apply_caps(long_rows)
    counts: dict[str, int] = {}
    for r in ranked_long:
        if r["status"] == EMITTED:
            counts[r["sector"]] = counts.get(r["sector"], 0) + 1

    return ChannelRankReport(
        as_of=as_of,
        created_at=now or datetime.now(UTC),
        sources=dict(sources or {}),
        policy=_policy(),
        long_ranked=ranked_long,
        short_ranked=short_rows,
        sector_counts=dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        warnings=warnings,
    )


def save_report(report: ChannelRankReport, out_dir: str | Path | None = None) -> Path:
    """Write the report as a new file and return its path. An existing report is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_report_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"channel_rank_{report.as_of}_{report.created_at:%Y%m%dT%H%M%SZ}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2)
        fh.write("\n")
    return path


def load_report(path: str | Path) -> ChannelRankReport:
    return ChannelRankReport.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
