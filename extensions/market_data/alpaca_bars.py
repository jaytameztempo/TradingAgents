"""Daily bars, and regular-session 4-hour bars, from Alpaca's market-data API, read-only.

Only Alpaca's market-data client is built here. It talks to data.alpaca.markets,
which serves prices and has no order endpoints, so nothing in this module can
place, change or cancel an order. Orders belong to a later execution adapter.

Keys come from ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment, never
from a file in git. Only paper keys are accepted: a live key has no business
in the process until the paper path has a track record.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd
import pytz
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

API_KEY_ENV = "ALPACA_API_KEY"
SECRET_KEY_ENV = "ALPACA_SECRET_KEY"

# Alpaca issues paper key IDs with this prefix; live key IDs start with "AK".
PAPER_KEY_PREFIX = "PK"

BAR_COLUMNS = ["open", "high", "low", "close", "volume", "trade_count", "vwap"]
FRAME_COLUMNS = ["symbol", "date", *BAR_COLUMNS]

FOUR_HOUR_COLUMNS = ["symbol", "timestamp", "date", "open", "high", "low", "close", "volume", "source_bars"]
THIRTY_MINUTES = TimeFrame(30, TimeFrameUnit.Minute)
# Regular session, New York time, split into two session-anchored 4-hour bars.
SESSION_OPEN, SESSION_SPLIT, SESSION_CLOSE = time(9, 30), time(13, 30), time(16, 0)
OPEN_OFFSET = pd.Timedelta(hours=9, minutes=30)
SPLIT_OFFSET = pd.Timedelta(hours=13, minutes=30)

MARKET_TZ = pytz.timezone("America/New_York")

# SPY, BRK.B, BF-B: one to five letters, optionally a share-class suffix.
_TICKER = re.compile(r"^[A-Z]{1,5}(?:[.-][A-Z]{1,2})?$")


class MissingAlpacaKeys(RuntimeError):
    """The Alpaca key variables are unset, blank, or not paper keys."""


class AlpacaDataError(RuntimeError):
    """Alpaca refused or failed the request after its own retries."""


def validate_ticker(symbol: str) -> str:
    """Return the symbol upper-cased, or raise ValueError if it is not a plain US ticker."""
    cleaned = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not _TICKER.fullmatch(cleaned):
        raise ValueError(f"not a valid US ticker: {symbol!r}")
    return cleaned


def market_today() -> date:
    """Today's date in New York, where the trading day is dated."""
    return datetime.now(MARKET_TZ).date()


def validate_date_range(start: date | str, end: date | str) -> tuple[date, date]:
    """Parse ISO dates and refuse a reversed range or an end date after today.

    A bar dated after today cannot exist; asking for one is a caller bug,
    and in a backtest it would be lookahead.
    """
    start_date, end_date = _as_date(start, "start"), _as_date(end, "end")
    if start_date > end_date:
        raise ValueError(f"start {start_date} is after end {end_date}")
    today = market_today()
    if end_date > today:
        raise ValueError(f"end {end_date} is after today ({today} in New York)")
    return start_date, end_date


def make_client(env: Mapping[str, str] | None = None) -> StockHistoricalDataClient:
    """Build the market-data client from paper keys in the environment.

    Error messages name the variables, never their values.
    """
    env = os.environ if env is None else env
    api_key = (env.get(API_KEY_ENV) or "").strip()
    secret_key = (env.get(SECRET_KEY_ENV) or "").strip()
    missing = [name for name, value in ((API_KEY_ENV, api_key), (SECRET_KEY_ENV, secret_key)) if not value]
    if missing:
        raise MissingAlpacaKeys(
            f"set {' and '.join(missing)} to your Alpaca paper keys (in .env, which git ignores)"
        )
    if not api_key.startswith(PAPER_KEY_PREFIX):
        raise MissingAlpacaKeys(
            f"{API_KEY_ENV} is not a paper key (paper key IDs start with {PAPER_KEY_PREFIX!r}); "
            "this project reads data with paper keys only"
        )
    return StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)


def fetch_daily_bars(
    symbols: str | Iterable[str],
    start: date | str,
    end: date | str,
    *,
    client: StockHistoricalDataClient | None = None,
    feed: DataFeed | str = DataFeed.IEX,
    adjustment: Adjustment | str = Adjustment.SPLIT,
    cache_dir: str | Path | None = None,
) -> pd.DataFrame:
    """Daily bars for each symbol from start to end, both inclusive.

    Returns one row per symbol and trading day, columns FRAME_COLUMNS, with
    ``date`` the New York trading date. A symbol Alpaca has no bars for is
    simply absent. IEX is the feed free accounts can query. Split adjustment
    keeps a split from looking like a crash without folding later dividends
    into earlier prices. Alpaca's client retries 429 and 504 responses itself.

    With ``cache_dir``, a range that ended before today is saved per symbol and
    read back instead of fetched; a range that includes today can still change.
    """
    tickers = _tickers(symbols)
    start_date, end_date = validate_date_range(start, end)
    feed, adjustment = DataFeed(feed), Adjustment(adjustment)

    cacheable = cache_dir is not None and end_date < market_today()
    paths = {t: _cache_path(cache_dir, t, start_date, end_date, feed, adjustment) for t in tickers} if cacheable else {}
    cached = [pd.read_csv(paths[t], parse_dates=["date"]) for t in tickers if t in paths and paths[t].exists()]
    to_fetch = [t for t in tickers if not (t in paths and paths[t].exists())]

    fetched = _empty_frame()
    if to_fetch:
        client = make_client() if client is None else client
        fetched = _request(client, to_fetch, start_date, end_date, feed, adjustment)
        for ticker, rows in fetched.groupby("symbol"):
            if ticker in paths:
                paths[ticker].parent.mkdir(parents=True, exist_ok=True)
                rows.to_csv(paths[ticker], index=False)

    frames = [f for f in (*cached, fetched) if not f.empty]
    if not frames:
        return _empty_frame()
    return pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"]).reset_index(drop=True)


def fetch_4hour_bars(
    symbols: str | Iterable[str],
    start: date | str,
    end: date | str,
    *,
    client: StockHistoricalDataClient | None = None,
    feed: DataFeed | str = DataFeed.IEX,
    adjustment: Adjustment | str = Adjustment.SPLIT,
    cache_dir: str | Path | None = None,
) -> pd.DataFrame:
    """Regular-session 4-hour bars for each symbol from start to end, both inclusive.

    Alpaca's own 4-hour bars are aligned to the clock (04:00, 08:00, 12:00,
    16:00 New York) and fold pre-market and after-hours trades in, so they are
    not used. Instead 30-minute bars are fetched, every bar starting before
    09:30 or at or after 16:00 is dropped, and the rest are rolled into two
    session-anchored bars per day: 09:30-13:30 and 13:30-16:00. No hourly
    bars are requested. Early-close sessions are not detected.

    Returns columns FOUR_HOUR_COLUMNS: ``timestamp`` is the bar's New York
    start time and ``date`` its trading date, both without a time zone;
    ``source_bars`` counts the 30-minute bars inside it. Caching works as in
    ``fetch_daily_bars``, in files of their own.
    """
    tickers = _tickers(symbols)
    start_date, end_date = validate_date_range(start, end)
    feed, adjustment = DataFeed(feed), Adjustment(adjustment)

    cacheable = cache_dir is not None and end_date < market_today()
    paths = {
        t: _cache_path(cache_dir, t, start_date, end_date, feed, adjustment).with_suffix(".4h_rth.csv")
        for t in tickers
    } if cacheable else {}
    cached = [pd.read_csv(paths[t], parse_dates=["timestamp", "date"]) for t in tickers if t in paths and paths[t].exists()]
    to_fetch = [t for t in tickers if not (t in paths and paths[t].exists())]

    fetched = _empty_4hour_frame()
    if to_fetch:
        client = make_client() if client is None else client
        raw = _get_bars(client, to_fetch, start_date, end_date, feed, adjustment, THIRTY_MINUTES)
        fetched = regular_session_4hour(raw)
        for ticker, rows in fetched.groupby("symbol"):
            if ticker in paths:
                paths[ticker].parent.mkdir(parents=True, exist_ok=True)
                rows.to_csv(paths[ticker], index=False)

    frames = [f for f in (*cached, fetched) if not f.empty]
    if not frames:
        return _empty_4hour_frame()
    return pd.concat(frames, ignore_index=True).sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def regular_session_4hour(raw: pd.DataFrame) -> pd.DataFrame:
    """Roll Alpaca's 30-minute bars, indexed by (symbol, timestamp), into regular-session 4-hour bars."""
    if raw.empty:
        return _empty_4hour_frame()
    frame = raw.reset_index()
    frame["start"] = frame["timestamp"].dt.tz_convert(MARKET_TZ).dt.tz_localize(None)
    clock = frame["start"].dt.time
    frame = frame[(clock >= SESSION_OPEN) & (clock < SESSION_CLOSE)].sort_values(["symbol", "start"])
    if frame.empty:
        return _empty_4hour_frame()
    frame["date"] = frame["start"].dt.normalize()
    afternoon = frame["start"].dt.time >= SESSION_SPLIT
    frame["timestamp"] = frame["date"] + pd.Series(
        [SPLIT_OFFSET if late else OPEN_OFFSET for late in afternoon], index=frame.index)
    rolled = (
        frame.groupby(["symbol", "timestamp"], sort=True)
        .agg(date=("date", "first"), open=("open", "first"), high=("high", "max"), low=("low", "min"),
             close=("close", "last"), volume=("volume", "sum"), source_bars=("close", "size"))
        .reset_index()
    )
    return rolled.reindex(columns=FOUR_HOUR_COLUMNS)


def _request(client, tickers, start_date, end_date, feed, adjustment) -> pd.DataFrame:
    return _tidy(_get_bars(client, tickers, start_date, end_date, feed, adjustment, TimeFrame.Day))


def _get_bars(client, tickers, start_date, end_date, feed, adjustment, timeframe) -> pd.DataFrame:
    request = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=timeframe,
        start=MARKET_TZ.localize(datetime.combine(start_date, time.min)),
        end=MARKET_TZ.localize(datetime.combine(end_date, time.max)),
        feed=feed,
        adjustment=adjustment,
    )
    try:
        bars = client.get_stock_bars(request)
    except APIError as exc:
        raise AlpacaDataError(f"Alpaca bars request failed ({exc.status_code}): {exc}") from exc
    return bars.df


def _tidy(raw: pd.DataFrame) -> pd.DataFrame:
    """Flatten Alpaca's (symbol, timestamp) index into symbol and trading-date columns."""
    if raw.empty:
        return _empty_frame()
    frame = raw.reset_index()
    frame["date"] = frame["timestamp"].dt.tz_convert(MARKET_TZ).dt.tz_localize(None).dt.normalize()
    return frame.reindex(columns=FRAME_COLUMNS)


def _empty_frame() -> pd.DataFrame:
    frame = pd.DataFrame(columns=FRAME_COLUMNS)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def _empty_4hour_frame() -> pd.DataFrame:
    frame = pd.DataFrame(columns=FOUR_HOUR_COLUMNS)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def _tickers(symbols: str | Iterable[str]) -> list[str]:
    symbols = [symbols] if isinstance(symbols, str) else list(symbols)
    if not symbols:
        raise ValueError("no tickers given")
    return list(dict.fromkeys(validate_ticker(s) for s in symbols))


def _as_date(value: date | str, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"{name} must be a date in YYYY-MM-DD form, got {value!r}") from None


def _cache_path(cache_dir, ticker, start_date, end_date, feed, adjustment) -> Path:
    return Path(cache_dir) / f"{ticker}_{start_date}_{end_date}_{feed.value}_{adjustment.value}.csv"
