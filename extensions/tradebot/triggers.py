"""The Tradebot's entry triggers: when a queued name may become a trade.

Every check first runs the gates, which take an item out of the queue and fail closed:

- regime: the route label must still be the item's bot (UP, DOWN or SIDE); no route is a block,
- expiry: past its expires_after session it expires,
- earnings: the earnings blackout's per-name check (cached SEC EDGAR filings, no fetch) must be clear or
  flagged_kept; removed blocks, and so does unknown or a cache older than earnings_cache_max_days unless the
  earnings_unknown setting is "allow",
- membership: the name must still pass the newest scan file of its kind (and category) dated on or before the
  check; dropping out expires it.

Then the two stages:

- eod (after the close, completed daily bars for as_of): ``confirmation.confirm``, the daily TTM Squeeze release
  in the trade's direction or the RSI(14) extreme, and light volume. A pass arms the item; a fail returns an
  armed item to waiting. An armed item whose 4-hour and 1-hour squeeze (``squeeze_gate``) has also fired on the
  completed session is triggered at once.
- intraday (during the session): for armed items only, the 4-hour and 1-hour squeeze on regular-session bars
  that are complete by now. Both fired in the trade's direction triggers the item.

Each filter can be switched off in the settings (squeeze_release, rsi_extreme, light_volume, squeeze_4h1h); the
switches in force are recorded with every check. With both daily triggers off the trigger part passes; with
squeeze_4h1h off an armed item is triggered at the eod stage.

Bars are passed in. Nothing here reads the network, writes a file or places an order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from extensions.status import confirmation, squeeze_gate
from extensions.status.confirmation import MarketData
from extensions.status.earnings_blackout import CLEAR, FLAGGED_KEPT, REMOVED, check_name
from extensions.tradebot import sources

EOD, INTRADAY = "eod", "intraday"
STAGES = (EOD, INTRADAY)
DATA_REASONS = (confirmation.NO_DAILY_BAR, confirmation.SHORT_HISTORY)
EXPECTED_SOURCE_BARS = {("1h", "15:30"): 1, ("4h", "09:30"): 8, ("4h", "13:30"): 5}  # every other 1h bar: 2
SPAN = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}
SESSION_CLOSE = (16, 0)


@dataclass
class Check:
    """One check of one item: the status it moves to (None: unchanged) and why."""
    item_id: str
    ticker: str
    stage: str
    status: str | None
    reason: str
    passed: bool = False
    gates: dict = field(default_factory=dict)
    daily: dict | None = None
    squeeze: dict | None = None
    filters: dict = field(default_factory=dict)

    def record(self) -> dict:
        """What the queue keeps of this check."""
        out = asdict(self)
        out.pop("item_id")
        out.pop("ticker")
        return out


# --- bars ----------------------------------------------------------------------

def _expected(timeframe: str, start: pd.Timestamp) -> int:
    return EXPECTED_SOURCE_BARS.get((timeframe, f"{start:%H:%M}"), 2 if timeframe == "1h" else 0)


def complete_session_bars(bars_30min: pd.DataFrame, timeframe: str, now: datetime | None) -> pd.DataFrame:
    """Regular-session 4h or 1h bars (squeeze_gate.roll_session_bars: extended hours dropped) with every bar still
    forming at `now` (New York, naive) dropped: its 30-minute parts must all have ended, and it must hold all of
    them. now None keeps every bar (a closed session)."""
    if now is not None and not bars_30min.empty:
        bars_30min = bars_30min[bars_30min["timestamp"] + timedelta(minutes=30) <= pd.Timestamp(now)]
    rolled = squeeze_gate.roll_session_bars(bars_30min, timeframe)
    if rolled.empty:
        return rolled
    ends = rolled["timestamp"].map(lambda t: min(t + SPAN[timeframe],
                                                 t.normalize() + timedelta(hours=SESSION_CLOSE[0])))
    full = rolled["source_bars"] == rolled["timestamp"].map(lambda t: _expected(timeframe, t))
    keep = full if now is None else full & (ends <= pd.Timestamp(now))
    return rolled[keep].reset_index(drop=True)


def squeeze_reads(bars_30min: pd.DataFrame, symbol: str, side: str, as_of: date,
                  now: datetime | None) -> squeeze_gate.SqueezeGate:
    """The 4-hour and 1-hour squeeze gate for one name on complete bars only."""
    if bars_30min is None or "symbol" not in bars_30min:  # no bars read: every timeframe reads as too short
        bars_30min = pd.DataFrame({c: pd.Series(dtype="datetime64[ns]" if c == "timestamp" else "float64")
                                   for c in ("symbol", "timestamp", "open", "high", "low", "close", "volume")})
    rows = bars_30min[bars_30min["symbol"] == symbol]
    reads = {tf: squeeze_gate.read_squeeze(complete_session_bars(rows, tf, now), tf, as_of)
             for tf in squeeze_gate.TIMEFRAMES}
    return squeeze_gate.gate(symbol, side, reads)


# --- gates ---------------------------------------------------------------------

def earnings_gate(item: dict, as_of: date, settings: dict, fundamentals_dir: Path) -> tuple[bool, dict]:
    """(ok, record) for the earnings check; unknown or stale fails unless earnings_unknown is allow."""
    check, warnings = check_name(item["ticker"], item["side"], as_of, fundamentals_dir)
    record = {"result": check.result, "earnings_date": check.earnings_date, "date_source": check.date_source,
              "sessions_away": check.sessions_away, "basis": check.basis, "fetched_at": check.fetched_at,
              "source": "SEC EDGAR company facts cache (no fetch)", "warnings": warnings}
    stale = None
    if check.fetched_at:
        try:
            age = (as_of - datetime.fromisoformat(check.fetched_at).date()).days
            if age > settings["earnings_cache_max_days"]:
                stale = f"fundamentals cache is {age} days old (over {settings['earnings_cache_max_days']})"
        except ValueError:
            stale = f"fundamentals cache fetched_at {check.fetched_at!r} is not a date"
    record["stale"] = stale
    if check.result == REMOVED:
        record["why"] = f"earnings {check.earnings_date}, {check.sessions_away} sessions away"
        return False, record
    if check.result in (CLEAR, FLAGGED_KEPT) and not stale:
        return True, record
    allow = settings["earnings_unknown"] == "allow"
    record["why"] = stale or f"earnings date unknown: {check.basis}"
    return allow, record


def membership_gate(item: dict, as_of: date) -> tuple[bool, str]:
    kind = item.get("kind")
    if kind not in sources.KINDS:
        return False, f"scan kind {kind!r} is not one the Tradebot checks"
    category = sources.category_of(kind, item.get("scan_file") or "")
    found = sources.newest_scan(kind, category, as_of, on_or_before=True)
    if found is None:
        return False, f"no {sources.KINDS[kind].label} scan file on or before {as_of}"
    path, data = found
    if item["ticker"] not in sources.passing_members(kind, data):
        return False, f"{item['ticker']} is not in the newest scan {path.name}"
    return True, path.name


def gates(item: dict, as_of: date, route_label: str | None, settings: dict,
          fundamentals_dir: Path) -> tuple[str | None, str, dict]:
    """(status to move to or None, reason, record): the first gate that fails, in order."""
    record: dict = {"route_label": route_label}
    if route_label is None:
        return "blocked", f"no route on or before {as_of}", record
    if route_label != item.get("bot"):
        return "blocked", f"market label is {route_label}; {item.get('bot')} names wait for {item.get('bot')}", record
    if item.get("expires_after") and as_of.isoformat() > item["expires_after"]:
        return "expired", f"past its last session {item['expires_after']}", record
    try:
        member, where = membership_gate(item, as_of)
    except sources.SourceError as exc:
        return "blocked", f"cannot check scan membership: {exc}", record
    record["scan"] = where
    if not member:
        return "expired", where, record
    ok, earnings = earnings_gate(item, as_of, settings, fundamentals_dir)
    record["earnings"] = earnings
    if not ok:
        return "blocked", f"earnings blackout: {earnings.get('why')}", record
    return None, "gates passed", record


# --- triggers --------------------------------------------------------------------

def daily_pass(read: confirmation.Confirmation, filters: dict) -> tuple[bool, list[str]]:
    """Whether the daily confirmation passes under the switches in force, and the reasons it does not."""
    data_problems = [r for r in read.reasons if r.startswith(DATA_REASONS)]
    if data_problems:
        return False, data_problems
    reasons = []
    wanted = [t for t, f in (("squeeze_release", "squeeze_release"), ("rsi", "rsi_extreme")) if filters[f]]
    if wanted and not any(t in read.triggers for t in wanted):
        reasons += [r for r in read.reasons if r.startswith(confirmation.AWAITING_TRIGGER)]
    if filters["light_volume"]:
        reasons += [r for r in read.reasons if r.startswith(confirmation.VOLUME_NOT_LIGHT)]
    return not reasons, reasons


def check_item(item: dict, stage: str, as_of: date, route_label: str | None, settings: dict, market: MarketData,
               bars_30min: pd.DataFrame, fundamentals_dir: Path, now: datetime | None = None) -> Check:
    """One item's check. now (New York, naive) limits intraday bars to complete ones; None at the eod stage."""
    filters = dict(settings["filters"])
    c = Check(item["id"], item["ticker"], stage, None, "", filters=filters)
    status, reason, c.gates = gates(item, as_of, route_label, settings, fundamentals_dir)
    if status:
        c.status, c.reason = status, reason
        return c
    side = item["side"]
    current = item.get("status")

    if stage == EOD:
        read = confirmation.confirm(item["ticker"], side, market, as_of)
        c.daily = read.to_dict()
        ok, why = daily_pass(read, filters)
        if not ok:
            c.status = "waiting" if current == "armed" else None
            c.reason = "; ".join(why)
            return c
        if not filters["squeeze_4h1h"]:
            c.status, c.passed, c.reason = "triggered", True, "daily triggers held (4-hour/1-hour squeeze is off)"
            return c
        gate = squeeze_reads(bars_30min, item["ticker"], side, as_of, None)
        c.squeeze = {"passed": gate.passed, "reason": gate.reason, **gate.to_dict()}
        if gate.passed:
            c.status, c.passed, c.reason = "triggered", True, "daily triggers held and the 4-hour/1-hour squeeze fired"
        else:
            c.status = "armed" if current != "armed" else None
            c.reason = f"armed: daily triggers held; waiting for the 4-hour/1-hour squeeze ({gate.reason})"
        return c

    if current != "armed":
        c.reason = "not armed; the intraday stage checks armed names only"
        return c
    if not filters["squeeze_4h1h"]:
        c.status, c.passed, c.reason = "triggered", True, "armed (4-hour/1-hour squeeze is off)"
        return c
    gate = squeeze_reads(bars_30min, item["ticker"], side, as_of, now)
    c.squeeze = {"passed": gate.passed, "reason": gate.reason, **gate.to_dict()}
    if gate.passed:
        c.status, c.passed, c.reason = "triggered", True, "the 4-hour/1-hour squeeze fired on complete bars"
    else:
        c.reason = gate.reason or "the 4-hour/1-hour squeeze has not fired"
    return c


def route_label(as_of: date, stage: str) -> tuple[str | None, str | None]:
    """(label, route file) for a check: the eod stage needs the route for as_of itself; the intraday stage uses
    the newest route on or before it (today's is written after the close). None when there is none."""
    found = sources.find_route(as_of) if stage == EOD else sources.route_on_or_before(as_of)
    return (found[1]["regime_label"], found[0].name) if found else (None, None)
