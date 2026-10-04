"""SCANBot build step 1: universe and liquidity on Alpaca, with a count for every gate.

The funnel runs the universe gates over Alpaca's asset records, then the
liquidity gates over consolidated (SIP) daily bars and sampled SIP quotes, and
writes a report: the thresholds, a stage count per gate, the survivors and a
reason for every removed name. It is not a TickerBasket and nothing routes it.

Failures are closed, with one exception. A bars failure, including Alpaca
refusing SIP, raises and no report is written. A quote failure skips only the
spread gate: the report is written with ``spread_method = "not_applied"``.
Nothing here places an order.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import requests

from extensions.market_data.alpaca_bars import MARKET_TZ, AlpacaDataError, fetch_daily_bars
from extensions.market_data.alpaca_quotes import fetch_quotes
from extensions.scanbot.liquidity import (
    SESSION_WINDOW,
    SPREAD_SESSIONS,
    combine_windows,
    gate_dollar_volume,
    gate_history,
    gate_share_volume,
    gate_spread,
    gate_zero_volume,
    liquidity_metrics,
    recent_sessions,
    window_spreads,
)
from extensions.scanbot.universe import UNIVERSE_GATES, Removal, universe_symbol

REPORT_VERSION = 1
FEED = "sip"
CALENDAR_SYMBOL = "SPY"
# About 30 trading days: 20 sessions plus room for holidays and the history check.
BAR_LOOKBACK_CALENDAR_DAYS = 45
BAR_CHUNK = 200
QUOTE_CHUNK = 100
SAMPLE_TIMES = (time(10, 30), time(12, 30), time(15, 30))  # New York time
SAMPLE_WINDOW_SECONDS = 10
SAMPLED, NOT_APPLIED = "sampled", "not_applied"
DATA_ERRORS = (AlpacaDataError, requests.exceptions.RequestException)


class CalendarError(RuntimeError):
    """The calendar symbol's bars do not cover enough sessions to measure liquidity."""


class _NoWindows(RuntimeError):
    """No quote sampling window has closed yet, so there is nothing to sample."""


@dataclass(frozen=True)
class Thresholds:
    min_dollar_volume_20: float
    min_share_volume_20: float
    max_spread_pct: float


PRESETS = {
    "DEFAULT": Thresholds(20_000_000, 500_000, 0.15),
    "AGGRESSIVE": Thresholds(5_000_000, 500_000, 0.30),
    "CONSERVATIVE": Thresholds(50_000_000, 500_000, 0.08),
}


@dataclass(frozen=True)
class StageCount:
    stage: str
    gate: str
    entered: int
    removed: int
    survived: int
    applied: bool = True


@dataclass(frozen=True)
class FunnelReport:
    as_of: date
    created_at: datetime
    preset: str
    thresholds: dict
    feed: str
    assets_source: str
    assets_fetched_at: datetime
    point_in_time: bool
    universe_count: int
    sessions: list[str]
    spread_method: str
    spread_sampling: dict
    stage_counts: list[StageCount]
    survivors: list[str]
    survivor_metrics: dict[str, dict]
    removed: list[Removal]
    warnings: list[str] = field(default_factory=list)
    spread_error: str | None = None
    report_version: int = REPORT_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        data["assets_fetched_at"] = self.assets_fetched_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> FunnelReport:
        if data.get("report_version") != REPORT_VERSION:
            raise ValueError(f"unsupported funnel report_version: {data.get('report_version')!r}")
        fields = dict(data)
        fields["as_of"] = date.fromisoformat(data["as_of"])
        fields["created_at"] = datetime.fromisoformat(data["created_at"])
        fields["assets_fetched_at"] = datetime.fromisoformat(data["assets_fetched_at"])
        fields["stage_counts"] = [StageCount(**s) for s in data["stage_counts"]]
        fields["removed"] = [Removal(**r) for r in data["removed"]]
        return cls(**fields)


class _Counter:
    """Runs gates in order and records a StageCount for each."""

    def __init__(self):
        self.counts: list[StageCount] = []
        self.removed: list[Removal] = []

    def apply(self, stage: str, gate: str, items: list, run: Callable[[list], tuple[list, list[Removal]]]) -> list:
        kept, removed = run(items)
        if len(kept) + len(removed) != len(items):
            raise AssertionError(f"gate {gate} lost track of names: {len(items)} in, {len(kept)} kept, {len(removed)} removed")
        self.counts.append(StageCount(stage, gate, len(items), len(removed), len(kept)))
        self.removed.extend(removed)
        return kept

    def skip(self, stage: str, gate: str, items: list) -> None:
        self.counts.append(StageCount(stage, gate, len(items), 0, len(items), applied=False))


def run_funnel(
    as_of: date,
    assets: list[dict],
    assets_fetched_at: datetime,
    assets_source: str,
    preset: str = "DEFAULT",
    *,
    bars_fetcher: Callable = fetch_daily_bars,
    quotes_fetcher: Callable = fetch_quotes,
    cache_dir: str | Path | None = None,
    created_at: datetime | None = None,
    now: datetime | None = None,
) -> FunnelReport:
    """Run every universe and liquidity gate for as_of. Bars failures raise; quote failures skip the spread gate."""
    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r}; choose from {', '.join(PRESETS)}")
    thresholds = PRESETS[preset]
    now = now or datetime.now(UTC)
    counter = _Counter()
    warnings = []

    # Stage 0: universe.
    records = assets
    for gate_name, gate in UNIVERSE_GATES:
        records = counter.apply("universe", gate_name, records, gate)
    symbols = [universe_symbol(a) for a in records]

    # Stage 2: liquidity. The calendar request is first, so a refused SIP feed stops the run before the big fetch.
    start = as_of - timedelta(days=BAR_LOOKBACK_CALENDAR_DAYS)
    calendar = bars_fetcher([CALENDAR_SYMBOL], start, as_of, feed=FEED, cache_dir=cache_dir)
    try:
        sessions = recent_sessions(calendar, as_of, SESSION_WINDOW)
    except ValueError as exc:
        raise CalendarError(str(exc)) from exc
    bars = _fetch_in_chunks(bars_fetcher, symbols, start, as_of, cache_dir)
    bars = bars[bars["date"] <= pd.Timestamp(as_of)]

    symbols = counter.apply("liquidity", "history", symbols, lambda s: gate_history(s, bars, sessions))
    symbols = counter.apply("liquidity", "zero_volume", symbols, lambda s: gate_zero_volume(s, bars, sessions))
    metrics = liquidity_metrics(bars, sessions)
    symbols = counter.apply(
        "liquidity", "share_volume", symbols,
        lambda s: gate_share_volume(s, metrics, thresholds.min_share_volume_20),
    )
    symbols = counter.apply(
        "liquidity", "dollar_volume", symbols,
        lambda s: gate_dollar_volume(s, metrics, thresholds.min_dollar_volume_20),
    )

    # The spread gate runs last, on volume survivors only.
    spread_sessions = sessions[-SPREAD_SESSIONS:]
    windows = _sample_windows(spread_sessions, now)
    spreads: dict[str, tuple[float, int]] = {}
    spread_error = None
    try:
        if symbols and not windows:
            raise _NoWindows("no sampling window has closed yet for the spread sessions")
        per_window = [
            window_spreads(_quotes_in_chunks(quotes_fetcher, symbols, window_start, window_end))
            for window_start, window_end in windows
        ] if symbols else []
        spreads = combine_windows(per_window)
    except (*DATA_ERRORS, _NoWindows) as exc:
        spread_error = f"{type(exc).__name__}: {exc}"

    if spread_error is None:
        spread_method = SAMPLED
        symbols = counter.apply(
            "liquidity", "spread", symbols, lambda s: gate_spread(s, spreads, thresholds.max_spread_pct)
        )
    else:
        spread_method = NOT_APPLIED
        counter.skip("liquidity", "spread", symbols)
        warnings.append(f"spread gate not applied: {spread_error}")

    point_in_time = as_of >= assets_fetched_at.astimezone(MARKET_TZ).date()
    if not point_in_time:
        warnings.append(
            f"the asset list was fetched {assets_fetched_at.isoformat()}, after the as-of date: "
            "names delisted since then are missing and later listings are included"
        )

    survivor_metrics = {
        s: {
            "avg_share_volume_20": round(float(metrics.loc[s, "avg_share_volume_20"]), 2),
            "avg_dollar_volume_20": round(float(metrics.loc[s, "avg_dollar_volume_20"]), 2),
            "median_spread_pct": round(spreads[s][0], 4) if s in spreads else None,
            "spread_windows": spreads[s][1] if s in spreads else 0,
        }
        for s in symbols
    }
    return FunnelReport(
        as_of=as_of,
        created_at=created_at or datetime.now(UTC),
        preset=preset,
        thresholds=asdict(thresholds),
        feed=FEED,
        assets_source=str(assets_source),
        assets_fetched_at=assets_fetched_at,
        point_in_time=point_in_time,
        universe_count=len(assets),
        sessions=[s.date().isoformat() for s in sessions],
        spread_method=spread_method,
        spread_sampling={
            "sessions": [s.date().isoformat() for s in spread_sessions],
            "times_new_york": [t.strftime("%H:%M") for t in SAMPLE_TIMES],
            "window_seconds": SAMPLE_WINDOW_SECONDS,
            "windows_requested": len(windows) if spread_method == SAMPLED else 0,
        },
        stage_counts=counter.counts,
        survivors=symbols,
        survivor_metrics=survivor_metrics,
        removed=counter.removed,
        warnings=warnings,
        spread_error=spread_error,
    )


def _fetch_in_chunks(bars_fetcher, symbols, start, end, cache_dir) -> pd.DataFrame:
    frames = [
        bars_fetcher(symbols[i:i + BAR_CHUNK], start, end, feed=FEED, cache_dir=cache_dir)
        for i in range(0, len(symbols), BAR_CHUNK)
    ]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame({"symbol": [], "date": pd.to_datetime([]), "close": [], "volume": []})
    return pd.concat(frames, ignore_index=True)


def _quotes_in_chunks(quotes_fetcher, symbols, start, end) -> pd.DataFrame:
    frames = [quotes_fetcher(symbols[i:i + QUOTE_CHUNK], start, end, feed=FEED) for i in range(0, len(symbols), QUOTE_CHUNK)]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["symbol", "timestamp", "bid_price", "ask_price"])
    return pd.concat(frames, ignore_index=True)


def _sample_windows(sessions: list[pd.Timestamp], now: datetime) -> list[tuple[datetime, datetime]]:
    """(start, end) for each sample time on each session, keeping only windows that have closed."""
    windows = []
    for session in sessions:
        for sample_time in SAMPLE_TIMES:
            start = MARKET_TZ.localize(datetime.combine(session.date(), sample_time))
            end = start + timedelta(seconds=SAMPLE_WINDOW_SECONDS)
            if end <= now:
                windows.append((start, end))
    return windows


def default_report_dir() -> Path:
    """~/.tradingagents/scanbot, beside the baskets and out of git."""
    return Path.home() / ".tradingagents" / "scanbot"


def save_report(report: FunnelReport, out_dir: str | Path | None = None) -> Path:
    """Write the report as a new file and return its path. An existing report is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_report_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"universe_{report.as_of}_{report.created_at:%Y%m%dT%H%M%SZ}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2)
        fh.write("\n")
    return path


def load_report(path: str | Path) -> FunnelReport:
    return FunnelReport.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
