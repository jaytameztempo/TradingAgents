"""SCANBot pivot quality filter on the names that passed the 4-hour confirmation.

Only names in the 4-hour report's ``confirmed`` lists are checked. Daily fails
and 4-hour fails are carried forward as fails and are never re-checked; the
daily gates and the 4-hour rules are not touched.

A name passes only if at least two of the four quality checks recorded in the
pivot report are true (SCANBot.md 4.1 and 4.2):

- distance to the SMA20, sloped the side's way, is inside 0.75 ATR
- RSI(14) is inside the side's band (UP 40-52, DOWN 48-60)
- volume SMA(5) is below 80% of the 20-day average
- the last two closes are off the extreme quartile (UP: not the bottom, DOWN: not the top)

The values are the pivot report's own. No bars are fetched. A check recorded
as missing counts as not true. Each recorded check is also re-derived from the
recorded metrics; a disagreement is a warning, never a gate. No score, ranking
or basket is built here. Nothing here places an order.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from extensions.scanbot.confirm_4h import Confirm4hReport
from extensions.scanbot.funnel import default_report_dir
from extensions.scanbot.pivot import (
    ATR_PERIOD,
    DOWN,
    MIN_QUALITY_CHECKS,
    Q_CLOSE_LOCATION,
    Q_RSI,
    Q_SMA20,
    Q_VOLUME,
    QUALITY_CHECKS,
    QUARTILE,
    RSI_PERIOD,
    SIDES,
    UP,
    PivotReport,
)

REPORT_VERSION = 1

TOO_FEW_CHECKS = f"quality_under_{MIN_QUALITY_CHECKS}_checks"
REASONS = (TOO_FEW_CHECKS,)

POLICY = {
    "source": "pivot report recorded quality checks; no bars fetched",
    "checks": list(QUALITY_CHECKS),
    "min_checks": MIN_QUALITY_CHECKS,
    "missing_check_counts_as": False,
    "checked": "4-hour confirmed names only; daily and 4-hour fails carried forward unchanged",
}

LABELS = {
    Q_SMA20: "SMA20 sloped the right way and inside 0.75 ATR",
    Q_RSI: "RSI(14) in band",
    Q_VOLUME: "volume SMA(5) under 80% of SMA(20)",
    Q_CLOSE_LOCATION: "last two closes off the extreme quartile",
}


@dataclass(frozen=True)
class QualityCall:
    side: str
    quality_pass: bool
    reasons: list[str]  # codes from REASONS; empty on a pass
    checks: dict[str, bool | None]  # as recorded in the pivot report
    checks_true: int
    detail: list[str]
    metrics: dict


def derive_checks(side: str, metrics: dict, policy: dict) -> dict[str, bool | None]:
    """Re-derive the four checks from the pivot report's recorded metrics and policy. None when an input is missing."""
    slope, distance = metrics.get("sma_20_sloped_right_way"), metrics.get("distance_to_sma20_atr")
    rsi, ratio = metrics.get(f"rsi_{RSI_PERIOD}"), metrics.get("volume_ratio_5_20")
    location = metrics.get("close_location_last_2")
    rsi_low, rsi_high = policy["rsi_bands"][side]
    return {
        Q_SMA20: None if slope is None or distance is None else bool(slope and distance <= policy["sma20_atr_max"]),
        Q_RSI: None if rsi is None else bool(rsi_low <= rsi <= rsi_high),
        Q_VOLUME: None if ratio is None else bool(ratio < policy["dryup_volume_ratio"]),
        Q_CLOSE_LOCATION: None if not location or len(location) != policy["close_location_sessions"] else bool(
            all(x > QUARTILE for x in location) if side == UP else all(x < 1 - QUARTILE for x in location)),
    }


def filter_quality(side: str, pivot_call: dict) -> QualityCall:
    """Apply the two-of-four rule to one name's recorded pivot call."""
    if side not in SIDES:
        raise ValueError(f"side must be {UP} or {DOWN}, not {side!r}")
    recorded = pivot_call.get("quality") or {}
    checks = {q: recorded.get(q) for q in QUALITY_CHECKS}
    true = [q for q in QUALITY_CHECKS if checks[q] is True]
    missing = [q for q in QUALITY_CHECKS if checks[q] is None]
    passed = len(true) >= MIN_QUALITY_CHECKS
    m = pivot_call.get("metrics") or {}
    metrics = {k: m.get(k) for k in (
        "sma_20", "sma_20_sloped_right_way", "distance_to_sma20_atr", f"atr_{ATR_PERIOD}",
        f"rsi_{RSI_PERIOD}", "volume_ratio_5_20", "close_location_last_2",
    )}
    detail = [f"{len(true)} of {len(QUALITY_CHECKS)} quality checks true; needs {MIN_QUALITY_CHECKS}"]
    detail += [f"{'yes' if checks[q] else 'missing' if checks[q] is None else 'no'}: {LABELS[q]}" for q in QUALITY_CHECKS]
    if missing:
        detail.append(f"recorded as missing, counted as not true: {', '.join(missing)}")
    return QualityCall(side, passed, [] if passed else [TOO_FEW_CHECKS], checks, len(true), detail, metrics)


# --- the run -----------------------------------------------------------------

@dataclass(frozen=True)
class QualityReport:
    as_of: date
    created_at: datetime
    confirm_report: str
    confirm_created_at: str
    pivot_report: str
    pivot_created_at: str
    policy: dict
    counts: dict[str, dict]  # per side: carried fails, checked, pass/fail, check counts, final pass/fail
    passed: dict[str, list[str]]  # daily pass, 4-hour pass and quality pass
    calls: dict[str, dict[str, dict]]  # side -> symbol -> QualityCall, 4-hour confirmed names only
    daily_fails: dict[str, list[str]]  # carried forward, never checked here
    confirm_4h_fails: dict[str, list[str]]  # carried forward, never checked here
    warnings: list[str] = field(default_factory=list)
    report_version: int = REPORT_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> QualityReport:
        if data.get("report_version") != REPORT_VERSION:
            raise ValueError(f"unsupported quality report_version: {data.get('report_version')!r}")
        fields = dict(data)
        fields["as_of"] = date.fromisoformat(data["as_of"])
        fields["created_at"] = datetime.fromisoformat(data["created_at"])
        return cls(**fields)


def _check_sources(confirm: Confirm4hReport, pivot: PivotReport) -> None:
    """The pivot report must be the one the 4-hour report was built from, and every confirmed name a daily pass."""
    if pivot.as_of != confirm.as_of:
        raise ValueError(f"pivot report is as of {pivot.as_of}, 4-hour report is as of {confirm.as_of}")
    if pivot.created_at.isoformat() != confirm.pivot_created_at:
        raise ValueError(
            f"pivot report created {pivot.created_at.isoformat()} is not the one the 4-hour report used "
            f"(created {confirm.pivot_created_at})"
        )
    for side in SIDES:
        calls = pivot.calls.get(side, {})
        bad = [s for s in confirm.confirmed.get(side, []) if not calls.get(s, {}).get("structure_pass")]
        if bad:
            raise ValueError(f"4-hour report confirms {side} names without a daily pass: {', '.join(bad)}")
        unchecked = [s for s in confirm.confirmed.get(side, []) if not confirm.calls[side].get(s, {}).get("confirm_pass")]
        if unchecked:
            raise ValueError(f"4-hour report lists {side} names without a passing call: {', '.join(unchecked)}")


def run_quality(
    confirm: Confirm4hReport,
    confirm_path: str | Path,
    pivot: PivotReport,
    pivot_path: str | Path,
    *,
    created_at: datetime | None = None,
) -> QualityReport:
    """Filter every 4-hour confirmed name on its recorded quality checks."""
    _check_sources(confirm, pivot)
    names = {side: sorted(confirm.confirmed.get(side, [])) for side in SIDES}
    calls = {side: {s: filter_quality(side, pivot.calls[side][s]) for s in names[side]} for side in SIDES}
    fails_4h = {side: sorted(s for s, c in confirm.calls.get(side, {}).items() if not c.get("confirm_pass"))
                for side in SIDES}
    daily_fails = {side: sorted(confirm.daily_fails.get(side, [])) for side in SIDES}

    warnings = list(confirm.warnings)
    for side in SIDES:
        for s in names[side]:
            derived = derive_checks(side, pivot.calls[side][s].get("metrics") or {}, pivot.policy)
            off = [q for q in QUALITY_CHECKS if derived[q] != calls[side][s].checks[q]]
            if off:
                warnings.append(f"{side} {s}: recorded {', '.join(off)} disagrees with the recorded metrics; "
                                "the recorded check was used")

    counts = {}
    for side in SIDES:
        passing = [c for c in calls[side].values() if c.quality_pass]
        entered = len(pivot.calls.get(side, {}))
        counts[side] = {
            "daily_entered": entered,
            "daily_fail_carried": len(daily_fails[side]),
            "fail_4h_carried": len(fails_4h[side]),
            "checked_quality": len(calls[side]),
            "quality_pass": len(passing),
            "quality_fail": len(calls[side]) - len(passing),
            "reason_counts": {code: sum(1 for c in calls[side].values() if code in c.reasons) for code in REASONS},
            "check_counts": {q: sum(1 for c in calls[side].values() if c.checks[q] is True) for q in QUALITY_CHECKS},
            "checks_true_histogram": {str(k): sum(1 for c in calls[side].values() if c.checks_true == k)
                                      for k in range(len(QUALITY_CHECKS) + 1)},
            "final_pass": len(passing),
            "final_fail": len(daily_fails[side]) + len(fails_4h[side]) + len(calls[side]) - len(passing),
        }
        c = counts[side]
        if c["final_pass"] + c["final_fail"] != entered:
            raise AssertionError(f"quality filter lost track of {side} names: {entered} in, {c}")

    return QualityReport(
        as_of=confirm.as_of,
        created_at=created_at or datetime.now(UTC),
        confirm_report=str(confirm_path),
        confirm_created_at=confirm.created_at.isoformat(),
        pivot_report=str(pivot_path),
        pivot_created_at=pivot.created_at.isoformat(),
        policy=dict(POLICY),
        counts=counts,
        passed={side: sorted(s for s, c in calls[side].items() if c.quality_pass) for side in SIDES},
        calls={side: {s: asdict(c) for s, c in calls[side].items()} for side in SIDES},
        daily_fails=daily_fails,
        confirm_4h_fails=fails_4h,
        warnings=warnings,
    )


def save_report(report: QualityReport, out_dir: str | Path | None = None) -> Path:
    """Write the report as a new file and return its path. An existing report is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_report_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"quality_{report.as_of}_{report.created_at:%Y%m%dT%H%M%SZ}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2)
        fh.write("\n")
    return path


def load_report(path: str | Path) -> QualityReport:
    return QualityReport.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
