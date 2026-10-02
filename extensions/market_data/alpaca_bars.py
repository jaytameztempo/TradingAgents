"""Daily bars from Alpaca's market-data API, read-only.

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
from alpaca.data.timeframe import TimeFrame

API_KEY_ENV = "ALPACA_API_KEY"
SECRET_KEY_ENV = "ALPACA_SECRET_KEY"

# Alpaca issues paper key IDs with this prefix; live key IDs start with "AK".
PAPER_KEY_PREFIX = "PK"

BAR_COLUMNS = ["open", "high", "low", "close", "volume", "trade_count", "vwap"]
FRAME_COLUMNS = ["symbol", "date", *BAR_COLUMNS]

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


def _request(client, tickers, start_date, end_date, feed, adjustment) -> pd.DataFrame:
    request = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame.Day,
        start=MARKET_TZ.localize(datetime.combine(start_date, time.min)),
        end=MARKET_TZ.localize(datetime.combine(end_date, time.max)),
        feed=feed,
        adjustment=adjustment,
    )
    try:
        bars = client.get_stock_bars(request)
    except APIError as exc:
        raise AlpacaDataError(f"Alpaca bars request failed ({exc.status_code}): {exc}") from exc
    return _tidy(bars.df)


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
