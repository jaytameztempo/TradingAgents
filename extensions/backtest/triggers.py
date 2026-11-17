"""The Tradebot's SIDE entry triggers, replayed on cached bars: when would a queued channel name have become a ticket?

Each PLAN name of a side handoff is queued on the run date (one item a symbol while it is active, as live), for
``expire_sessions`` sessions counting the run date. Every session, in the live order (``extensions/tradebot/
triggers.py``, called through its own functions):

1. gates: the route's regime label on that day must still be SIDE (else removed), and the name must still pass the
   newest pivot-confirmed channel report of its side on or before that day (live's membership gate; else expired);
2. intraday: an item armed at the previous close is triggered when the 4-hour and 1-hour squeeze
   (``squeeze_reads``, complete regular-session bars up to that day's close) has fired in its direction;
3. end of day: ``confirmation.confirm`` on that session, judged by ``daily_pass`` with the filter switches. A pass
   arms the item (and triggers it at once when the squeeze has fired too, or when the squeeze filter is off); a
   fail returns an armed item to waiting.

A trigger is approximated at that session's close: the ticket's DAY limit goes out the next session (live, an
intraday trigger could fill the same afternoon). An item past ``expire_sessions`` sessions expires. Without cached
30-minute bars for a name the squeeze filter is taken as off for it, and the item is flagged. The earnings gate is
not simulated (filing dates are not point in time). Bars are read from the cache; nothing is fetched or written.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from extensions.backtest.bars import BarCache
from extensions.status import confirmation
from extensions.status.confirmation import MarketData
from extensions.tradebot import config as tradebot_config
from extensions.tradebot.triggers import daily_pass, squeeze_reads

FILTERS = tradebot_config.FILTERS
DEFAULT_FILTERS = dict(tradebot_config.DEFAULTS["filters"])
DEFAULT_EXPIRE = int(tradebot_config.DEFAULTS["expire_sessions"])
WAITING, ARMED, TRIGGERED, EXPIRED, REMOVED = "waiting", "armed", "triggered", "expired", "removed"
_30MIN = r"_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}_[a-z]+_[a-z]+\.30min\.csv"


def settings_defaults() -> tuple[dict, int]:
    """(filters, expire_sessions) from the Tradebot settings in settings.json; the defaults when unreadable."""
    try:
        s = tradebot_config.load()
        return dict(s["filters"]), int(s["expire_sessions"])
    except Exception:  # noqa: BLE001 - a missing or broken settings file means the defaults here
        return dict(DEFAULT_FILTERS), DEFAULT_EXPIRE


@dataclass
class Item:
    """One queued name."""
    symbol: str
    side: str
    queued_on: date
    source: object  # the pit.Saved handoff (or channel report) that queued it
    status: str = WAITING
    armed_ever: bool = False
    checks: int = 0
    triggered_on: date | None = None
    reason: str = ""
    squeeze_missing: bool = False
    ended_on: date | None = None  # the last session it was checked (triggered, removed, expired)
    history: list[str] = field(default_factory=list)


class Bars30:
    """Cached 30-minute bars (``*.30min.csv``, as ``fetch_30min_bars`` saves them) by symbol."""

    def __init__(self, home: str | Path):
        self.folder = Path(home) / "cache" / "bars"
        self._frames: dict[str, pd.DataFrame] = {}

    def get(self, symbol: str) -> pd.DataFrame:
        if symbol not in self._frames:
            name = re.compile(re.escape(symbol) + _30MIN + "$")
            parts = []
            for path in sorted(self.folder.glob(f"{symbol}_*.30min.csv")) if self.folder.is_dir() else []:
                if name.fullmatch(path.name):
                    try:
                        parts.append(pd.read_csv(path, parse_dates=["timestamp"]))
                    except (OSError, ValueError, pd.errors.ParserError):
                        continue
            parts = [p for p in parts if not p.empty]
            frame = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
            if not frame.empty:
                frame["symbol"] = symbol
                frame = frame.drop_duplicates(subset="timestamp", keep="last").sort_values("timestamp")
                frame = frame.reset_index(drop=True)
            self._frames[symbol] = frame
        return self._frames[symbol]


def market_for(cache: BarCache, symbol: str) -> MarketData:
    """The daily bars ``confirmation.confirm`` reads, for one name (its 4-hour flag is recorded only: none here)."""
    raw = cache.raw_daily(symbol)
    if raw.empty or "volume" not in raw.columns:
        daily = pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume"])
    else:
        daily = raw[["date", "open", "high", "low", "close", "volume"]].copy()
        daily.insert(0, "symbol", symbol)
    return MarketData(daily, pd.DataFrame(), "backtest cache")


def run_queue(items: list[Item], sessions_for: Callable[[str], list[date]], label_on: Callable[[date], str | None],
              member_on: Callable[[str, str, date], tuple[bool, str]], cache: BarCache, bars30: Bars30,
              filters: dict, expire_sessions: int) -> None:
    """Walk every item through its sessions, setting status, triggered_on and reason in place."""
    for item in items:
        market = market_for(cache, item.symbol)
        thirty = bars30.get(item.symbol)
        squeeze_on = filters["squeeze_4h1h"]
        if squeeze_on and thirty.empty:
            item.squeeze_missing = True
        days = [d for d in sessions_for(item.symbol) if d >= item.queued_on][:expire_sessions]
        for day in days:
            item.checks += 1
            item.ended_on = day
            label = label_on(day)
            if label != "SIDE":
                item.status, item.reason = REMOVED, f"regime {label or 'none'} on {day}, not SIDE"
                break
            ok, why = member_on(item.symbol, item.side, day)
            if not ok:
                item.status, item.reason = EXPIRED, why
                break
            fired = None
            if item.status == ARMED and squeeze_on and not item.squeeze_missing:  # intraday: armed names only
                gate = squeeze_reads(thirty, item.symbol, item.side, day, None)
                if gate.passed:
                    item.status, item.triggered_on = TRIGGERED, day
                    item.reason = "armed; the 4-hour/1-hour squeeze fired"
                    break
                fired = gate
            read = confirmation.confirm(item.symbol, item.side, market, day)
            passed, reasons = daily_pass(read, filters)
            if not passed:
                item.status = WAITING
                item.reason = "; ".join(reasons) or "daily triggers did not hold"
                item.history.append(f"{day}: {item.reason}")
                continue
            item.armed_ever = True
            if not squeeze_on or item.squeeze_missing:
                item.status, item.triggered_on = TRIGGERED, day
                item.reason = ("daily triggers held (4-hour/1-hour squeeze is off)" if not squeeze_on else
                               "daily triggers held (no cached 30-minute bars: squeeze taken as off)")
                break
            gate = fired or squeeze_reads(thirty, item.symbol, item.side, day, None)
            if gate.passed:
                item.status, item.triggered_on = TRIGGERED, day
                item.reason = "daily triggers held and the 4-hour/1-hour squeeze fired"
                break
            item.status = ARMED
            item.reason = f"armed: waiting for the 4-hour/1-hour squeeze ({gate.reason})"
            item.history.append(f"{day}: {item.reason}")
        else:
            if item.status != TRIGGERED:
                last = item.reason
                item.status = EXPIRED
                item.reason = (f"expired after {len(days)} session(s) without a trigger"
                               + (f" (last: {last})" if last else ""))
