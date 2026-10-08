"""SCANBot 4-hour confirmation of the daily pivot passes in a saved pivot report.

Only names in the pivot report's ``passed`` lists are checked. A daily fail is
carried forward as a fail and is never re-checked; the daily gates are not
touched. Bars are regular-session 4-hour bars (09:30-13:30 and 13:30-16:00 New
York, built from 30-minute bars, no extended hours, no hourly bars) dated on or
before as_of.

The window runs from the daily impulse-end date (the swing high for UP, the
swing low for DOWN) through as_of. UP passes only if all hold:

- coverage: at least one 4-hour bar on every session in the window
- the highest 4-hour high in the window is on the daily swing date; a strictly
  higher high on a later session means the 4-hour chart shows no pullback
- the last 4-hour close is below the low of that peak bar

DOWN is the mirror: the lowest 4-hour low is on the swing date and the last
4-hour close is above the high of that trough bar. Sessions come from the
calendar symbol's 4-hour bars. Missing data is a fail, with every reason that
applies. No score, ranking or basket is built here. Nothing here places an order.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from extensions.market_data.alpaca_bars import SESSION_CLOSE, SESSION_OPEN, SESSION_SPLIT, fetch_4hour_bars
from extensions.scanbot.funnel import CALENDAR_SYMBOL, FEED, CalendarError, default_report_dir
from extensions.scanbot.pivot import DOWN, SIDES, UP, PivotReport

REPORT_VERSION = 1

NO_DAILY_SWING = "no_daily_swing_in_pivot_report"
NO_4H_BARS = "no_4h_bars"
MISSING_SESSIONS = "4h_missing_sessions"
NEW_EXTREME = "4h_new_extreme_after_swing_date"
NOT_PAST_EXTREME_BAR = "4h_close_not_past_extreme_bar"
REASONS = (NO_DAILY_SWING, NO_4H_BARS, MISSING_SESSIONS, NEW_EXTREME, NOT_PAST_EXTREME_BAR)

POLICY = {
    "feed": FEED,
    "adjustment": "split",
    "bars": "regular_session_4h_from_30min",
    "session_segments": [
        [SESSION_OPEN.strftime("%H:%M"), SESSION_SPLIT.strftime("%H:%M")],
        [SESSION_SPLIT.strftime("%H:%M"), SESSION_CLOSE.strftime("%H:%M")],
    ],
    "extended_hours": False,
    "hourly_bars_used": False,
    "window": "daily impulse-end date through as_of",
    "calendar_symbol": CALENDAR_SYMBOL,
    "rules": {
        UP: ["a bar on every session", "highest 4h high on the swing date", "last 4h close below that bar's low"],
        DOWN: ["a bar on every session", "lowest 4h low on the swing date", "last 4h close above that bar's high"],
    },
    "checked": "pivot report passed lists only; daily fails carried forward unchanged",
}


@dataclass(frozen=True)
class ConfirmCall:
    side: str
    confirm_pass: bool
    reasons: list[str]  # codes from REASONS; empty on a pass
    detail: list[str]
    metrics: dict


def confirm_4h(side: str, bars: pd.DataFrame, swing_date: date | None, sessions: list[date]) -> ConfirmCall:
    """Check one daily pass on its 4-hour bars. ``sessions`` are the trading dates from swing_date through as_of.

    The caller has already dropped bars after as_of.
    """
    if side not in SIDES:
        raise ValueError(f"side must be {UP} or {DOWN}, not {side!r}")
    if swing_date is None:
        return ConfirmCall(side, False, [NO_DAILY_SWING], ["the pivot report has no daily impulse-end swing"], {})
    window = bars[bars["date"] >= pd.Timestamp(swing_date)].sort_values("timestamp").reset_index(drop=True)
    metrics: dict = {"swing_date": swing_date.isoformat(), "sessions_expected": len(sessions), "bars_4h": len(window)}
    if window.empty:
        return ConfirmCall(side, False, [NO_4H_BARS],
                           [f"no regular-session 4-hour bars from {swing_date} through as_of"], metrics)

    codes, detail = [], []
    covered = {d.date() for d in window["date"]}
    missing = [d for d in sessions if d not in covered]
    metrics["missing_sessions"] = [d.isoformat() for d in missing]
    if missing:
        codes.append(MISSING_SESSIONS)
        detail.append(f"no 4-hour bar on {len(missing)} session(s): {', '.join(d.isoformat() for d in missing)}")

    up = side == UP
    word = "high" if up else "low"
    at = int(window["high"].idxmax() if up else window["low"].idxmin())  # first bar at the extreme
    extreme = window.iloc[at]
    last = window.iloc[-1]
    extreme_price = float(extreme["high"] if up else extreme["low"])
    edge = float(extreme["low"] if up else extreme["high"])  # the peak bar's low, or the trough bar's high
    last_close = float(last["close"])
    metrics.update({
        f"extreme_4h_{word}": round(extreme_price, 4),
        "extreme_4h_bar": extreme["timestamp"].isoformat(),
        f"extreme_bar_{'low' if up else 'high'}": round(edge, 4),
        "last_4h_bar": last["timestamp"].isoformat(),
        "last_4h_close": round(last_close, 4),
        "bars_after_extreme": len(window) - 1 - at,
    })

    if extreme["date"].date() != swing_date:
        codes.append(NEW_EXTREME)
        detail.append(
            f"4-hour {word} {extreme_price:.2f} on {extreme['timestamp']:%Y-%m-%d %H:%M} is past the "
            f"daily swing {word} on {swing_date}"
        )
    if not (last_close < edge if up else last_close > edge):
        codes.append(NOT_PAST_EXTREME_BAR)
        detail.append(
            f"last 4-hour close {last_close:.2f} is not {'below' if up else 'above'} the "
            f"{'peak' if up else 'trough'} bar's {'low' if up else 'high'} {edge:.2f}"
        )
    if not codes:
        leg = "pullback" if up else "bounce"
        detail = [
            f"4-hour {word} {extreme_price:.2f} at {extreme['timestamp']:%Y-%m-%d %H:%M}, on the daily swing date",
            f"{leg} visible: last 4-hour close {last_close:.2f} {'below' if up else 'above'} "
            f"{'peak' if up else 'trough'} bar {'low' if up else 'high'} {edge:.2f}",
        ]
    return ConfirmCall(side, not codes, codes, detail, metrics)


def _swing_date(call: dict) -> date | None:
    end = (call.get("metrics") or {}).get("impulse_end") or {}
    return date.fromisoformat(end["date"]) if end.get("date") else None


# --- the run -----------------------------------------------------------------

@dataclass(frozen=True)
class Confirm4hReport:
    as_of: date
    created_at: datetime
    pivot_report: str
    pivot_created_at: str
    policy: dict
    counts: dict[str, dict]  # per side: daily in/pass/fail, 4-hour checked/pass/fail, reason counts
    confirmed: dict[str, list[str]]  # daily pass and 4-hour pass
    calls: dict[str, dict[str, dict]]  # side -> symbol -> ConfirmCall, daily passes only
    daily_fails: dict[str, list[str]]  # carried forward as fails, never checked here
    warnings: list[str] = field(default_factory=list)
    report_version: int = REPORT_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Confirm4hReport:
        if data.get("report_version") != REPORT_VERSION:
            raise ValueError(f"unsupported confirm4h report_version: {data.get('report_version')!r}")
        fields = dict(data)
        fields["as_of"] = date.fromisoformat(data["as_of"])
        fields["created_at"] = datetime.fromisoformat(data["created_at"])
        return cls(**fields)


def _daily_names(pivot: PivotReport) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(passes, fails) per side, taken from the pivot report and checked against its own calls."""
    passes, fails = {}, {}
    for side in SIDES:
        calls = pivot.calls.get(side, {})
        passes[side] = sorted(pivot.passed.get(side, []))
        bad = [s for s in passes[side] if not calls.get(s, {}).get("structure_pass")]
        if bad:
            raise ValueError(f"pivot report lists {side} passes without a passing call: {', '.join(bad)}")
        fails[side] = sorted(s for s, c in calls.items() if not c.get("structure_pass"))
    return passes, fails


def run_confirm_4h(
    pivot: PivotReport,
    pivot_path: str | Path,
    *,
    bars_fetcher: Callable = fetch_4hour_bars,
    cache_dir: str | Path | None = None,
    created_at: datetime | None = None,
) -> Confirm4hReport:
    """Check every daily pass of the pivot report on 4-hour bars. A bars failure raises."""
    as_of = pivot.as_of
    passes, fails = _daily_names(pivot)
    swings = {side: {s: _swing_date(pivot.calls[side][s]) for s in passes[side]} for side in SIDES}
    symbols = [s for side in SIDES for s in passes[side]]
    known = [d for side in SIDES for d in swings[side].values() if d is not None]

    by_symbol: dict[str, pd.DataFrame] = {}
    calendar: list[date] = []
    warnings = list(pivot.warnings)
    if symbols and known:
        start = min(known)
        bars = bars_fetcher([CALENDAR_SYMBOL, *symbols], start, as_of, feed=FEED, cache_dir=cache_dir)
        if not bars.empty:
            bars = bars[bars["date"] <= pd.Timestamp(as_of)]
        groups = dict(tuple(bars.groupby("symbol"))) if not bars.empty else {}
        cal = groups.get(CALENDAR_SYMBOL)
        if cal is None or cal.empty:
            raise CalendarError(f"no 4-hour bars for calendar symbol {CALENDAR_SYMBOL} from {start} to {as_of}")
        calendar = sorted({d.date() for d in cal["date"]})
        if calendar[-1] != as_of:
            warnings.append(f"calendar symbol {CALENDAR_SYMBOL} has no 4-hour bar on {as_of}; last is {calendar[-1]}")
        by_symbol = {s: groups[s] for s in symbols if s in groups}

    empty = pd.DataFrame({"symbol": [], "timestamp": pd.to_datetime([]), "date": pd.to_datetime([]),
                          "open": [], "high": [], "low": [], "close": [], "volume": []})
    calls = {
        side: {
            s: confirm_4h(side, by_symbol.get(s, empty), swings[side][s],
                          [d for d in calendar if swings[side][s] is not None and d >= swings[side][s]])
            for s in passes[side]
        }
        for side in SIDES
    }

    counts = {}
    for side in SIDES:
        passing = [s for s, c in calls[side].items() if c.confirm_pass]
        counts[side] = {
            "daily_entered": len(pivot.calls.get(side, {})),
            "daily_pass": len(passes[side]),
            "daily_fail_carried": len(fails[side]),
            "checked_4h": len(calls[side]),
            "pass_4h": len(passing),
            "fail_4h": len(calls[side]) - len(passing),
            "reason_counts": {code: sum(1 for c in calls[side].values() if code in c.reasons) for code in REASONS},
            "final_pass": len(passing),
            "final_fail": len(fails[side]) + len(calls[side]) - len(passing),
        }
        c = counts[side]
        if c["checked_4h"] != c["daily_pass"] or c["final_pass"] + c["final_fail"] != c["daily_entered"]:
            raise AssertionError(f"4-hour confirmation lost track of {side} names: {c}")

    return Confirm4hReport(
        as_of=as_of,
        created_at=created_at or datetime.now(UTC),
        pivot_report=str(pivot_path),
        pivot_created_at=pivot.created_at.isoformat(),
        policy=dict(POLICY),
        counts=counts,
        confirmed={side: sorted(s for s, c in calls[side].items() if c.confirm_pass) for side in SIDES},
        calls={side: {s: asdict(c) for s, c in calls[side].items()} for side in SIDES},
        daily_fails=fails,
        warnings=warnings,
    )


def save_report(report: Confirm4hReport, out_dir: str | Path | None = None) -> Path:
    """Write the report as a new file and return its path. An existing report is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_report_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"confirm4h_{report.as_of}_{report.created_at:%Y%m%dT%H%M%SZ}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2)
        fh.write("\n")
    return path


def load_report(path: str | Path) -> Confirm4hReport:
    return Confirm4hReport.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
