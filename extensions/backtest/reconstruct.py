"""Rebuild the pipeline as of a past date, for dates that never had a scan run.

The real pipeline code runs here (universe funnel, financial gates, regime, category baskets, router, channel scans,
channel rank, side handoff). Each orchestrator gets a bars fetcher that serves daily bars dated on or before the
as-of date only, so nothing sees the future. The reports are written in the live formats, but under
``<home>/backtest/reconstructed`` (``RECON_DIR``), never in the live folders, and every file is stamped at 21:00 UTC
on the as-of date (17:00 New York, after the close): known on that date, traded from the next session's open.

What is not point in time, and is recorded as a warning on each date:
- the universe and the borrow flags come from the newest saved asset snapshot (names delisted since are missing);
- the spread gate is not applied (spreads cannot be rebuilt from bars);
- fundamentals come from the cached SEC files (facts filed on or before the as-of date count); a name with no cached
  file fails the gates unless SEC download is on (``fetch_fundamentals``), which downloads it the live way;
- when the ranked side handoff refuses a name with no mapped sector, the unranked side handoff is used instead.

Daily bars missing from the cache are fetched once for the whole range with ``fetch_daily_bars`` (sip, split) and
saved in the live cache; 4-hour bars for channel passes are fetched (and cached) by the channel scans' own fetcher.
Nothing here places an order.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from extensions.backtest.bars import BarCache
from extensions.backtest.pit import (DONE, HOLIDAY, MANIFEST, MANIFEST_VERSION, RECON_DIR, read_manifest,
                                     recon_home, saved_dates, weekdays)
from extensions.market_data import alpaca_assets
from extensions.market_data.alpaca_bars import fetch_4hour_bars, fetch_daily_bars
from extensions.regime import regime_bot
from extensions.research import ranked_side_handoff, side_handoff
from extensions.router import strategy_router
from extensions.scanbot import channel_rank, financials, funnel, long_channel, short_channel
from extensions.scanbot import fundamentals as fx
from extensions.scanbot.universe import UNIVERSE_GATES, universe_symbol
from extensions.scans import scan_down_category, scan_up_category
from extensions.scans.basket import save_basket
from extensions.scans.categories import DEFAULT_CATEGORY, params_for
from extensions.scans.category_inputs import load_inputs

FETCH_LOG = "fetch_log.json"
LOOKBACK_DAYS = 400  # calendar days of history before the first as-of date (the scans' lookback)
STAMP_TIME = time(21, 0)  # UTC: after the New York close
FEED, ADJUSTMENT = "sip", "split"
REGIME_SYMBOLS = strategy_router.REGIME_SYMBOLS
FOLDERS = ("scanbot", "regimes", "baskets", "routes", "handoffs", "plans")


class ReconstructError(RuntimeError):
    """A date cannot be rebuilt (no asset snapshot, no SPY bars, ...)."""


# --- folders and the manifest --------------------------------------------------------------

def write_manifest(home: str | Path, manifest: dict) -> None:
    _write_json(recon_home(home) / MANIFEST, manifest)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def pending_dates(home: str | Path, start: date, end: date, *, rebuild: bool = False,
                  all_dates: bool = False) -> list[date]:
    """Weekdays in the range to rebuild: not saved (unless all_dates), not already rebuilt (unless rebuild)."""
    saved = set() if all_dates else saved_dates(home)
    done = read_manifest(home)["dates"]
    return [d for d in weekdays(start, end) if d not in saved and (rebuild or d.isoformat() not in done)]


def stamp_for(as_of: date) -> datetime:
    return datetime.combine(as_of, STAMP_TIME, tzinfo=UTC)


# --- bars ----------------------------------------------------------------------------------

SLACK_START, SLACK_END = timedelta(days=7), timedelta(days=5)  # weekends and holidays at either edge


def _need_from(frame: pd.DataFrame, attempt: list[str] | None, start: date, end: date) -> date | None:
    """The first date to fetch for one symbol, or None when the cache (or an earlier attempt) already covers
    start..end. A symbol whose history already reaches back to start (or was already tried back to start) only
    needs the days after what it holds, so a later end date fetches a short tail, not the whole range again."""
    tried_from = date.fromisoformat(attempt[0]) if attempt else None
    tried_to = date.fromisoformat(attempt[1]) if attempt else None
    history_ok = tried_from is not None and tried_from <= start
    last = None
    if not frame.empty:
        first, last = frame["date"].iloc[0].date(), frame["date"].iloc[-1].date()
        history_ok = history_ok or first <= start + SLACK_START
    if not history_ok:
        return start
    reached = max(d for d in (last, tried_to) if d is not None) if (last or tried_to) else None
    if reached is None:
        return start
    if reached >= end - SLACK_END:
        return None
    return reached + timedelta(days=1)


def ensure_bars(home: str | Path, symbols: list[str], start: date, end: date, *,
                fetcher: Callable = fetch_daily_bars, chunk: int = funnel.BAR_CHUNK,
                progress: Callable[[str], None] | None = None) -> dict[str, pd.DataFrame]:
    """Daily bars for ``symbols`` from start to end, saving what is fetched in the live cache. Only what the cache
    lacks is fetched: the whole range for a symbol with no history back to start, else only the days after its last
    cached bar; a range already tried (``fetch_log.json``) is not fetched again. Returns the cached and fetched bars
    per symbol (the full merged history; callers cut it)."""
    home = Path(home)
    cache = BarCache(home)
    log_path = recon_home(home) / FETCH_LOG
    try:
        tried = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tried = {}
    frames = {s: cache.raw_daily(s) for s in symbols}
    groups: dict[date, list[str]] = {}
    for s in symbols:
        need = _need_from(frames[s], tried.get(s), start, end)
        if need is not None:
            groups.setdefault(need, []).append(s)
    total = sum(len(g) for g in groups.values())
    if progress:
        tails = sum(len(g) for d, g in groups.items() if d > start)
        progress(f"daily bars: {len(symbols) - total} of {len(symbols)} symbols cached, fetching {total} "
                 f"({tails} only the days after their last cached bar)")
    done = 0
    for need, members in sorted(groups.items()):
        for i in range(0, len(members), chunk):
            part = members[i:i + chunk]
            fetched = fetcher(part, need, end, feed=FEED, adjustment=ADJUSTMENT, cache_dir=cache.folder)
            for s in part:
                old = tried.get(s)
                tried[s] = [min(old[0], start.isoformat()) if old else start.isoformat(), end.isoformat()]
                rows = fetched[fetched["symbol"] == s] if not fetched.empty else fetched
                if not rows.empty:
                    merged = pd.concat([frames[s], rows], ignore_index=True) if not frames[s].empty else rows.copy()
                    merged["date"] = pd.to_datetime(merged["date"])
                    frames[s] = merged.drop_duplicates(subset="date", keep="last").sort_values("date").reset_index(
                        drop=True)
            _write_json(log_path, tried)
            done += len(part)
            if progress:
                progress(f"daily bars: fetched {done} of {total}")
    return frames


def master_frame(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    parts = []
    for symbol, frame in frames.items():
        if frame.empty:
            continue
        part = frame.copy()
        part["symbol"] = symbol
        parts.append(part)
    if not parts:
        return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume"])
    master = pd.concat(parts, ignore_index=True)
    master["date"] = pd.to_datetime(master["date"])
    return master.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)


def sliced_fetcher(master: pd.DataFrame, as_of: date) -> Callable:
    """A ``fetch_daily_bars`` stand-in over ``master`` that never returns a bar dated after ``as_of``."""
    cutoff = pd.Timestamp(as_of)

    def fetch(symbols, start, end, **_kwargs) -> pd.DataFrame:
        symbols = [symbols] if isinstance(symbols, str) else list(symbols)
        last = min(pd.Timestamp(end), cutoff)
        rows = master[master["symbol"].isin(symbols) & (master["date"] >= pd.Timestamp(start))
                      & (master["date"] <= last)]
        return rows.reset_index(drop=True)

    return fetch


def no_quotes(*_args, **_kwargs):
    """The spread gate's quotes cannot be rebuilt for a past date: raising here makes the funnel skip that gate."""
    raise funnel.DATA_ERRORS[0]("historical spread sampling is not rebuilt in a backtest")


def cached_4hour_fetcher(cache_dir: Path) -> Callable:
    def fetch(symbols, start, end, **_kwargs):
        return fetch_4hour_bars(symbols, start, end, feed=FEED, adjustment=ADJUSTMENT, cache_dir=cache_dir)
    return fetch


# --- the inputs ----------------------------------------------------------------------------

def newest_snapshot(home: str | Path) -> Path:
    found = sorted((Path(home) / "assets").glob("assets_*.json"))
    if not found:
        raise ReconstructError(f"no asset snapshot in {Path(home) / 'assets'}; run the universe scan once")
    return found[-1]


def universe_symbols(assets: list[dict]) -> list[str]:
    """The symbols that pass the asset-record gates (common stock, tradable, exchange ...): the funnel's stage 0."""
    counter = funnel._Counter()
    records = assets
    for gate_name, gate in UNIVERSE_GATES:
        records = counter.apply("universe", gate_name, records, gate)
    return [universe_symbol(a) for a in records]


def fundamentals_lookups(ctx: Context, symbols: list[str]) -> tuple[dict[str, fx.Lookup], dict]:
    """A Lookup per symbol, and where they came from ({fetch, cached, fetched, missing}).

    A cached file is used whatever its age: the financial gates keep only facts filed on or before the as-of, so a
    newer download would not change the result. With ``ctx.fetch_fundamentals``, a symbol with no cached file is
    downloaded the live step's way (``fx.load_fundamentals``: SEC's ticker map, the company-facts download, the
    rate limit, ``fx.write_file`` into the shared fundamentals cache). A symbol is tried once per job; one that
    cannot be downloaded fails the gates, as live."""
    folder = ctx.home / "fundamentals"
    lookups: dict[str, fx.Lookup] = {}
    to_fetch = []
    for symbol in symbols:
        existing = fx.read_file(fx.file_path(folder, symbol))
        if existing is not None:
            lookups[symbol] = fx.Lookup(symbol, existing, fx.CACHE)
        elif not ctx.fetch_fundamentals:
            lookups[symbol] = fx.Lookup(symbol, None, failure="no cached fundamentals (SEC download is off)")
        elif ctx.ticker_map_error:
            lookups[symbol] = fx.Lookup(symbol, None, failure=f"SEC ticker map unavailable ({ctx.ticker_map_error})")
        elif symbol in ctx.sec_tried:
            lookups[symbol] = fx.Lookup(symbol, None, failure="SEC download failed earlier in this rebuild")
        else:
            to_fetch.append(symbol)
    if to_fetch:
        ctx.sec_tried.update(to_fetch)
        ctx.log(f"  SEC fundamentals: downloading {len(to_fetch)} missing file(s)")
        try:
            lookups.update(fx.load_fundamentals(
                to_fetch, folder=folder, ticker_map_loader=ctx.ticker_map, facts_fetcher=ctx.facts_fetcher,
                throttle=ctx.throttle, progress=_sec_progress(ctx.log)))
        except fx.TickerMapUnavailable as exc:
            ctx.ticker_map_error = str(exc)
            ctx.log(f"  SEC ticker map unavailable ({exc}); continuing on cached fundamentals only")
            for symbol in to_fetch:
                lookups[symbol] = fx.Lookup(symbol, None, failure=f"SEC ticker map unavailable ({exc})")
    counts = {
        "fetch": ctx.fetch_fundamentals,
        "cached": sum(1 for lk in lookups.values() if lk.file is not None and lk.source == fx.CACHE),
        "fetched": sum(1 for lk in lookups.values() if lk.file is not None and lk.source != fx.CACHE),
        "missing": sum(1 for lk in lookups.values() if lk.file is None),
    }
    return lookups, counts


def _sec_progress(log: Callable[[str], None]) -> Callable[[int, int], None]:
    def progress(done: int, total: int) -> None:
        if done == total or done % 50 == 0:
            log(f"  SEC fundamentals: {done} of {total}")
    return progress


@dataclass
class Context:
    home: Path
    master: pd.DataFrame
    snapshot: Path
    assets: list[dict]
    assets_fetched_at: datetime
    four_hour_fetcher: Callable
    category: int = DEFAULT_CATEGORY
    log: Callable[[str], None] = print
    sessions: set[date] = field(default_factory=set)
    # SEC download of missing fundamentals (off unless asked for), with the live step's loader and its parts.
    fetch_fundamentals: bool = False
    facts_fetcher: Callable = fx.fetch_company_facts
    ticker_map_loader: Callable = fx.load_ticker_map
    throttle: fx.Throttle = field(default_factory=fx.Throttle)
    sec_tried: set[str] = field(default_factory=set)
    ticker_map_error: str | None = None
    _ticker_map: dict | None = None

    def ticker_map(self) -> dict:
        """SEC's ticker map, loaded once per job (a failure raises fx.TickerMapUnavailable)."""
        if self._ticker_map is None:
            self._ticker_map = self.ticker_map_loader()
        return self._ticker_map


def build_context(home: str | Path, master: pd.DataFrame, *, four_hour_fetcher: Callable | None = None,
                  log: Callable[[str], None] = print, fetch_fundamentals: bool = False,
                  facts_fetcher: Callable | None = None, ticker_map_loader: Callable | None = None,
                  throttle: fx.Throttle | None = None) -> Context:
    home = Path(home)
    snapshot = newest_snapshot(home)
    fetched_at, assets = alpaca_assets.load_snapshot(snapshot)
    spy = master[master["symbol"] == "SPY"]
    return Context(home=home, master=master, snapshot=snapshot, assets=assets, assets_fetched_at=fetched_at,
                   four_hour_fetcher=four_hour_fetcher or cached_4hour_fetcher(home / "cache" / "bars"),
                   log=log, sessions={d.date() for d in spy["date"]}, fetch_fundamentals=fetch_fundamentals,
                   facts_fetcher=facts_fetcher or fx.fetch_company_facts,
                   ticker_map_loader=ticker_map_loader or fx.load_ticker_map, throttle=throttle or fx.Throttle())


# --- one date ------------------------------------------------------------------------------

def reconstruct_date(as_of: date, ctx: Context) -> dict:
    """Rebuild the pipeline for one as-of date. Returns the manifest entry (status, label, files, warnings)."""
    if as_of not in ctx.sessions:
        if ctx.sessions and as_of < max(ctx.sessions):
            return {"status": HOLIDAY, "label": None, "files": [], "warnings": [], "built_at": _now()}
        raise ReconstructError(f"no SPY bar on {as_of}: no bars for that date yet")

    out = recon_home(ctx.home)
    for folder in FOLDERS:
        (out / folder).mkdir(parents=True, exist_ok=True)
    stamp = stamp_for(as_of)
    bars = sliced_fetcher(ctx.master, as_of)
    files: list[str] = []
    warnings: list[str] = []

    def keep(path) -> Path:
        path = Path(path)
        files.append(str(path.relative_to(out)))
        return path

    # 1. Universe and liquidity (no spread gate), then 2. the financial gates (cached SEC files, plus downloads of
    # missing ones when SEC download is on).
    universe = funnel.run_funnel(as_of, ctx.assets, ctx.assets_fetched_at, str(ctx.snapshot), bars_fetcher=bars,
                                 quotes_fetcher=no_quotes, created_at=stamp, now=stamp)
    universe_path = keep(funnel.save_report(universe, out / "scanbot"))
    lookups, fundamentals_counts = fundamentals_lookups(ctx, universe.survivors)
    fin = financials.run_financials(universe, universe_path, lookups, fundamentals_dir=ctx.home / "fundamentals",
                                    created_at=stamp)
    fin_path = keep(financials.save_report(fin, out / "scanbot"))

    # 3. Regime per symbol.
    labels = {}
    for symbol in REGIME_SYMBOLS:
        label = regime_bot.label_symbol(bars([symbol], as_of - timedelta(days=LOOKBACK_DAYS), as_of), symbol, as_of,
                                        created_at=stamp)
        labels[symbol] = (keep(regime_bot.save_label(label, out / "regimes")), label)

    # 4. The UP and DOWN category baskets, before the router (an UP or DOWN route needs its basket).
    inputs = load_inputs(fin_path, ctx.snapshot)
    found = {}
    for scan, module in (("up", scan_up_category), ("down", scan_down_category)):
        basket = module.run_scan(inputs, params_for(scan, ctx.category), bars_fetcher=bars, created_at=stamp)
        found[scan] = (keep(save_basket(basket, out / "baskets")), basket)

    # 5. The route.
    decision = strategy_router.route(as_of, labels, up=found["up"], down=found["down"], created_at=stamp,
                                     category=ctx.category)
    route_path = keep(strategy_router.save_decision(decision, out / "routes"))

    # 6. The channel scans (4-hour pivot confirmation on real 4-hour bars).
    fin_report = financials.load_report(fin_path)
    long_report = long_channel.run_long_channel(fin_report, fin_path, bars_fetcher=bars,
                                                four_hour_fetcher=ctx.four_hour_fetcher, created_at=stamp)
    long_path = keep(long_channel.save_report(long_report, out / "scanbot"))
    short_report = short_channel.run_short_channel(
        fin_report, fin_path, short_channel.borrow_flags(ctx.assets), str(ctx.snapshot), ctx.assets_fetched_at,
        bars_fetcher=bars, four_hour_fetcher=ctx.four_hour_fetcher, created_at=stamp)
    short_path = keep(short_channel.save_report(short_report, out / "scanbot"))

    # 7. Rank, then the side handoff on SIDE days.
    long_dict, short_dict = _read(long_path), _read(short_path)
    rank = channel_rank.rank_channels(long_dict, short_dict, _read(fin_path), _read(universe_path), now=stamp,
                                      sources={"long_channel": str(long_path), "short_channel": str(short_path),
                                               "financials": str(fin_path), "universe": str(universe_path)})
    rank_path = keep(channel_rank.save_report(rank, out / "scanbot"))
    handoff_kind = None
    if decision.regime_label == strategy_router.SIDE:
        try:
            handoff = ranked_side_handoff.build_ranked_side_handoff(
                _read(rank_path), rank_path, long_report, long_path, short_report, short_path,
                _read(route_path), route_path, created_at=stamp)
            path, _plans = ranked_side_handoff.save_ranked_side_handoff(handoff, out / "handoffs", out / "plans")
            handoff_kind = "ranked"
        except (ValueError, strategy_router.MissingInput) as exc:
            warnings.append(f"ranked side handoff refused ({exc}); used the unranked side handoff instead")
            handoff = side_handoff.build_side_handoff(long_report, long_path, short_report, short_path, decision,
                                                      route_path, created_at=stamp)
            path, _plans = side_handoff.save_side_handoff(handoff, out / "handoffs", out / "plans")
            handoff_kind = "unranked"
        keep(path)

    warnings += [f"universe: {w}" for w in universe.warnings]
    if fundamentals_counts["missing"]:
        why = ("no SEC file could be downloaded for them" if ctx.fetch_fundamentals
               else "they have no cached SEC file and SEC download is off")
        warnings.append(f"{fundamentals_counts['missing']} of {len(lookups)} liquidity survivors fail the financial "
                        f"gates: {why}")
    ctx.log(f"{as_of}: {decision.regime_label} (SPY {labels['SPY'][1].label}, QQQ {labels['QQQ'][1].label}); "
            f"universe {len(universe.survivors)}; UP basket {len(found['up'][1].members)}, "
            f"DOWN basket {len(found['down'][1].members)}; long channel {len(long_report.members)}, "
            f"short channel {len(short_report.members)}; side handoff {handoff_kind or '-'}; fundamentals "
            f"{fundamentals_counts['cached']} cached, {fundamentals_counts['fetched']} downloaded, "
            f"{fundamentals_counts['missing']} missing")
    return {"status": DONE, "label": decision.regime_label, "files": files, "warnings": warnings,
            "handoff": handoff_kind, "fundamentals": fundamentals_counts, "built_at": _now()}


def forget_date(home: str | Path, manifest: dict, as_of: date) -> None:
    """Delete one rebuilt date's files (rebuild), from the rebuild folder only."""
    out = recon_home(home)
    entry = manifest["dates"].pop(as_of.isoformat(), None) or {}
    for rel in entry.get("files", []):
        path = (out / rel).resolve()
        if path.is_relative_to(out.resolve()) and path.is_file():
            path.unlink()
    for path in (out / "plans").glob(f"*_{as_of.isoformat()}_*.md"):
        path.unlink()


def _read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _now() -> str:
    return datetime.now(UTC).isoformat()
