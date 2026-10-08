"""4-hour confirmation of the planned names in a saved ranked side handoff.

Only the handoff's ``planned_long`` and ``planned_short`` names are checked;
NOT_RANKED names are left out. The plans, channel scans, ranker, router and
bots are not touched. Bars are regular-session 4-hour bars (09:30-13:30 and
13:30-16:00 New York, built from 30-minute bars by ``fetch_4hour_bars``, no
extended hours, no hourly bars) dated on or before as_of.

Support, resistance and the daily ATR(14) are the handoff's own recorded
levels. The thresholds are the channel scans' own:

- long fade: d = (last 4h close - support) / ATR. Passes only if d is at most
  0.6 (still near support) and at least -0.25 (not broken through support).
- short fade: d = (resistance - last 4h close) / ATR. Passes only if d is at
  most 0.6 (still near resistance) and at least -0.25 (not broken through it).

The last 4-hour bar must be dated as_of. Missing data is a fail, with every
reason that applies. No score, ranking or plan is changed. Nothing here places
an order.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

from extensions.market_data.alpaca_bars import SESSION_CLOSE, SESSION_OPEN, SESSION_SPLIT, fetch_4hour_bars
from extensions.research.handoff import PLAN, default_handoff_dir
from extensions.router.strategy_router import MissingInput
from extensions.scanbot import long_channel, short_channel
from extensions.scanbot.funnel import CALENDAR_SYMBOL, FEED, CalendarError, default_report_dir

REPORT_VERSION = 1
LONG, SHORT = "long", "short"
SIDES = (LONG, SHORT)
SETUP = {LONG: "LONG_FADE", SHORT: "SHORT_FADE"}
LINE = {LONG: "support", SHORT: "resistance"}
NEAR_ATR_MAX = {LONG: long_channel.PIVOT_ATR_MAX, SHORT: short_channel.PIVOT_ATR_MAX}  # 0.6
BREAK_ATR_MAX = {LONG: long_channel.BREAK_ATR_MAX, SHORT: short_channel.BREAK_ATR_MAX}  # 0.25
# A week of calendar days reaches back past a weekend and a holiday to the as_of session.
BAR_LOOKBACK_CALENDAR_DAYS = 7

MISSING_LEVELS = "missing_levels_in_handoff"
NO_4H_BARS = "no_4h_bars"
STALE_LAST_BAR = "last_4h_bar_not_on_as_of"
TOO_FAR = "4h_close_over_0.6atr_from_line"
BROKEN = "4h_close_over_0.25atr_through_line"
REASONS = (MISSING_LEVELS, NO_4H_BARS, STALE_LAST_BAR, TOO_FAR, BROKEN)

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
    "bars_dated_on_or_before": "as_of",
    "calendar_symbol": CALENDAR_SYMBOL,
    "levels": "support, resistance and daily atr_14 as recorded in the ranked side handoff",
    "near_atr_max": NEAR_ATR_MAX[LONG],
    "break_atr_max": BREAK_ATR_MAX[LONG],
    "rules": {
        LONG: ["last 4h bar dated as_of", "(close - support) / ATR <= 0.6", "(close - support) / ATR >= -0.25"],
        SHORT: ["last 4h bar dated as_of", "(resistance - close) / ATR <= 0.6",
                "(resistance - close) / ATR >= -0.25"],
    },
    "checked": "planned_long and planned_short names only; plans are not rewritten",
}


@dataclass(frozen=True)
class SideConfirmCall:
    side: str
    confirm_pass: bool
    reasons: list[str]  # codes from REASONS; empty on a pass
    detail: list[str]
    metrics: dict


def confirm_side_4h(side: str, bars: pd.DataFrame, levels: dict, as_of: date) -> SideConfirmCall:
    """Check one planned name's last 4-hour close against its channel line. The caller has dropped bars after as_of."""
    if side not in SIDES:
        raise ValueError(f"side must be {LONG} or {SHORT}, not {side!r}")
    line_word = LINE[side]
    line, atr = levels.get(line_word), levels.get("atr_14")
    metrics: dict = {line_word: line, "atr_14": atr, "bars_4h": len(bars)}
    codes, detail = [], []
    if line is None or atr is None or not atr > 0:
        codes.append(MISSING_LEVELS)
        detail.append(f"the handoff has no usable {line_word} or atr_14 (got {line!r}, {atr!r})")
    if bars.empty:
        codes.append(NO_4H_BARS)
        detail.append(f"no regular-session 4-hour bars on or before {as_of}")
        return SideConfirmCall(side, False, codes, detail, metrics)

    last = bars.sort_values("timestamp").iloc[-1]
    last_close = float(last["close"])
    metrics.update({"last_4h_bar": last["timestamp"].isoformat(), "last_4h_close": round(last_close, 4)})
    if last["date"].date() != as_of:
        codes.append(STALE_LAST_BAR)
        detail.append(f"last 4-hour bar is {last['timestamp']:%Y-%m-%d %H:%M}, not on {as_of}")
    if MISSING_LEVELS in codes:
        return SideConfirmCall(side, False, codes, detail, metrics)

    # Positive is on the channel side of the line, negative is through it.
    d = (last_close - line) / atr if side == LONG else (line - last_close) / atr
    metrics[f"distance_to_{line_word}_atr"] = round(d, 4)
    where = "above" if side == LONG else "below"
    if d > NEAR_ATR_MAX[side]:
        codes.append(TOO_FAR)
        detail.append(f"last 4-hour close {last_close:.2f} is {d:.2f} ATR {where} {line_word} {line:.2f}, "
                      f"over {NEAR_ATR_MAX[side]:g}")
    if d < -BREAK_ATR_MAX[side]:
        codes.append(BROKEN)
        detail.append(f"last 4-hour close {last_close:.2f} is {-d:.2f} ATR through {line_word} {line:.2f}, "
                      f"over {BREAK_ATR_MAX[side]:g}")
    if not codes:
        detail = [f"last 4-hour close {last_close:.2f} at {last['timestamp']:%Y-%m-%d %H:%M} is {d:+.2f} ATR "
                  f"from {line_word} {line:.2f}; inside -{BREAK_ATR_MAX[side]:g} to +{NEAR_ATR_MAX[side]:g}"]
    return SideConfirmCall(side, not codes, codes, detail, metrics)


# --- the ranked side handoff -------------------------------------------------

def latest_ranked_side_handoff(as_of: date, folder: str | Path | None = None) -> tuple[Path, dict]:
    """The newest ``ranked_side_handoff_{as_of}_*.json``, by the created_at inside each file."""
    folder = Path(folder) if folder is not None else default_handoff_dir()
    found = []
    for path in sorted(folder.glob(f"ranked_side_handoff_{as_of}_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(data["created_at"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MissingInput(f"cannot read ranked side handoff {path}: {exc}") from exc
        if data.get("as_of") == as_of.isoformat():
            found.append((created, path, data))
    if not found:
        raise MissingInput(f"no ranked side handoff for {as_of} in {folder}")
    _, path, data = max(found, key=lambda item: item[0])
    return path, data


def planned_names(handoff: dict) -> dict[str, list[str]]:
    """The planned names per side, checked against each name's recorded setup and status."""
    planned = {LONG: list(handoff.get("planned_long", [])), SHORT: list(handoff.get("planned_short", []))}
    names = handoff.get("names", {})
    for side in SIDES:
        bad = [s for s in planned[side]
               if names.get(s, {}).get("setup") != SETUP[side] or names.get(s, {}).get("status") != PLAN]
        if bad:
            raise ValueError(f"ranked handoff plans {side} names without a {SETUP[side]} PLAN entry: {', '.join(bad)}")
    both = set(planned[LONG]) & set(planned[SHORT])
    if both:
        raise ValueError(f"ranked handoff plans names on both sides: {', '.join(sorted(both))}")
    return planned


# --- the run -----------------------------------------------------------------

@dataclass(frozen=True)
class SideConfirm4hReport:
    as_of: date
    created_at: datetime
    ranked_handoff: str
    ranked_handoff_created_at: str
    regime_label: str
    policy: dict
    counts: dict  # per side and total: checked, pass, fail, reason counts
    confirmed: dict[str, list[str]]
    failed: dict[str, list[str]]
    calls: dict[str, dict[str, dict]]  # side -> symbol -> SideConfirmCall
    plans_rewritten: bool = False
    orders_placed: bool = False
    warnings: list[str] = field(default_factory=list)
    report_version: int = REPORT_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> SideConfirm4hReport:
        if data.get("report_version") != REPORT_VERSION:
            raise ValueError(f"unsupported side_confirm4h report_version: {data.get('report_version')!r}")
        fields = dict(data)
        fields["as_of"] = date.fromisoformat(data["as_of"])
        fields["created_at"] = datetime.fromisoformat(data["created_at"])
        return cls(**fields)


def run_side_confirm_4h(
    handoff: dict,
    handoff_path: str | Path,
    *,
    bars_fetcher: Callable = fetch_4hour_bars,
    cache_dir: str | Path | None = None,
    created_at: datetime | None = None,
) -> SideConfirm4hReport:
    """Check every planned name of the ranked side handoff on 4-hour bars. A bars failure raises."""
    as_of = date.fromisoformat(handoff["as_of"])
    planned = planned_names(handoff)
    symbols = [s for side in SIDES for s in planned[side]]
    warnings = list(handoff.get("warnings", []))

    groups: dict[str, pd.DataFrame] = {}
    if symbols:
        start = as_of - timedelta(days=BAR_LOOKBACK_CALENDAR_DAYS)
        bars = bars_fetcher([CALENDAR_SYMBOL, *symbols], start, as_of, feed=FEED, cache_dir=cache_dir)
        if not bars.empty:
            bars = bars[bars["date"] <= pd.Timestamp(as_of)]
            groups = dict(tuple(bars.groupby("symbol")))
        cal = groups.get(CALENDAR_SYMBOL)
        if cal is None or cal.empty:
            raise CalendarError(f"no 4-hour bars for calendar symbol {CALENDAR_SYMBOL} from {start} to {as_of}")
        if cal["date"].max().date() != as_of:
            warnings.append(f"calendar symbol {CALENDAR_SYMBOL} has no 4-hour bar on {as_of}; "
                            f"last is {cal['date'].max().date()}")

    empty = pd.DataFrame({"symbol": [], "timestamp": pd.to_datetime([]), "date": pd.to_datetime([]),
                          "open": [], "high": [], "low": [], "close": [], "volume": []})
    names = handoff["names"]
    calls = {side: {s: confirm_side_4h(side, groups.get(s, empty), names[s].get("levels") or {}, as_of)
                    for s in planned[side]} for side in SIDES}

    counts: dict = {}
    for side in SIDES:
        passing = [s for s, c in calls[side].items() if c.confirm_pass]
        counts[side] = {
            "planned": len(planned[side]),
            "checked_4h": len(calls[side]),
            "pass_4h": len(passing),
            "fail_4h": len(calls[side]) - len(passing),
            "reason_counts": {code: sum(1 for c in calls[side].values() if code in c.reasons) for code in REASONS},
        }
        c = counts[side]
        if c["checked_4h"] != c["planned"] or c["pass_4h"] + c["fail_4h"] != c["planned"]:
            raise AssertionError(f"side 4-hour confirmation lost track of {side} names: {c}")
    counts["total"] = {k: sum(counts[side][k] for side in SIDES) for k in ("planned", "checked_4h", "pass_4h", "fail_4h")}

    return SideConfirm4hReport(
        as_of=as_of,
        created_at=created_at or datetime.now(UTC),
        ranked_handoff=str(handoff_path),
        ranked_handoff_created_at=handoff["created_at"],
        regime_label=handoff.get("regime_label"),
        policy=dict(POLICY),
        counts=counts,
        confirmed={side: [s for s in planned[side] if calls[side][s].confirm_pass] for side in SIDES},
        failed={side: [s for s in planned[side] if not calls[side][s].confirm_pass] for side in SIDES},
        calls={side: {s: asdict(c) for s, c in calls[side].items()} for side in SIDES},
        warnings=warnings,
    )


def save_report(report: SideConfirm4hReport, out_dir: str | Path | None = None) -> Path:
    """Write the report as a new file and return its path. An existing report is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_report_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"side_confirm4h_{report.as_of}_{report.created_at:%Y%m%dT%H%M%SZ}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2)
        fh.write("\n")
    return path


def load_report(path: str | Path) -> SideConfirm4hReport:
    return SideConfirm4hReport.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
