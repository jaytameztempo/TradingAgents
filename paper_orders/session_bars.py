"""30-minute and daily bars from Alpaca's market-data REST API, read-only, with no Alpaca package, for the paper
tickets: ``extensions.status.squeeze_gate`` rolls 30-minute bars into regular-session 4-hour and 1-hour bars, and
``extensions.status.confirmation`` reads daily bars for the confirmation filters and the SPY/QQQ ADX.

``extensions.market_data.alpaca_bars`` cannot be used where Alpaca's trading client must stay out: it loads
alpaca.data, and alpaca.data loads alpaca.trading (so does any ``extensions.market_data`` import, through that
package's __init__, which is why this module sits in ``paper_orders``). This module reads the same 30-minute bars
over HTTPS GET with the standard library, from data.alpaca.markets only (prices, no order endpoints), with paper
keys only (the check in ``paper_orders.snapshot``). Alpaca's own hourly and 4-hour bars are clock-aligned and fold
extended hours in, so they are not requested.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from paper_orders.snapshot import paper_keys

DATA_URL = "https://data.alpaca.markets"
BARS_PATH = "/v2/stocks/bars"
FEED, ADJUSTMENT = "iex", "split"  # as alpaca_bars and the scans use
DAILY_FEED = "sip"  # the channel scans' daily feed (extensions.scanbot.funnel.FEED)
PAGE_LIMIT = 10_000
MARKET_TZ = ZoneInfo("America/New_York")
THIRTY_MINUTE_COLUMNS = ["symbol", "timestamp", "open", "high", "low", "close", "volume"]
DAILY_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume"]


class BarsReadFailed(RuntimeError):
    """The market-data API could not be read."""


class DataReader:
    """Alpaca's market-data API over HTTPS GET. Public surface: bars_30min, bars_daily."""

    def __init__(self, api_key: str, secret_key: str,
                 opener: Callable[..., Any] = urllib.request.urlopen, timeout: float = 30.0) -> None:
        self.__headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key,
                          "Accept": "application/json"}
        self.__opener = opener
        self.__timeout = timeout

    def bars_30min(self, symbols: Iterable[str], start: date, end: date) -> pd.DataFrame:
        """30-minute bars from start to end, both inclusive New York dates; columns THIRTY_MINUTE_COLUMNS, timestamp
        the bar's New York start time without a time zone."""
        rows = self.__rows(symbols, "30Min", start, end, FEED)
        if not rows:
            return _empty()
        frame = pd.DataFrame(rows, columns=THIRTY_MINUTE_COLUMNS)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(MARKET_TZ).dt.tz_localize(None)
        return frame.sort_values(["symbol", "timestamp"]).reset_index(drop=True)

    def bars_daily(self, symbols: Iterable[str], start: date, end: date, feed: str = DAILY_FEED) -> pd.DataFrame:
        """Daily bars from start to end, both inclusive New York dates; columns DAILY_COLUMNS, date the session's New
        York date (midnight, no time zone). SIP and split-adjusted by default, as the channel scans read them."""
        rows = self.__rows(symbols, "1Day", start, end, feed)
        if not rows:
            return _empty(DAILY_COLUMNS, "date")
        frame = pd.DataFrame(rows, columns=DAILY_COLUMNS)
        frame["date"] = (pd.to_datetime(frame["date"], utc=True).dt.tz_convert(MARKET_TZ).dt.tz_localize(None)
                         .dt.normalize())
        return frame.sort_values(["symbol", "date"]).reset_index(drop=True)

    def __rows(self, symbols: Iterable[str], timeframe: str, start: date, end: date, feed: str) -> list[tuple]:
        """(symbol, t, o, h, l, c, v) for every bar, page by page."""
        tickers = sorted(dict.fromkeys(symbols))
        if not tickers:
            return []
        params = {
            "symbols": ",".join(tickers), "timeframe": timeframe,
            "start": datetime.combine(start, time.min, MARKET_TZ).isoformat(),
            "end": datetime.combine(end, time(23, 59, 59), MARKET_TZ).isoformat(),
            "feed": feed, "adjustment": ADJUSTMENT, "limit": PAGE_LIMIT, "sort": "asc",
        }
        rows, token = [], None
        while True:
            page = self.__get({**params, **({"page_token": token} if token else {})})
            for symbol, bars in (page.get("bars") or {}).items():
                rows += [(symbol, b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in bars or []]
            token = page.get("next_page_token")
            if not token:
                break
        return rows

    def __get(self, params: dict) -> dict:
        req = urllib.request.Request(f"{DATA_URL}{BARS_PATH}?{urllib.parse.urlencode(params)}",
                                     headers=self.__headers, method="GET")
        try:
            with self.__opener(req, timeout=self.__timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise BarsReadFailed(f"GET {BARS_PATH}: HTTP {exc.code}: {exc.reason}") from exc
        except OSError as exc:
            raise BarsReadFailed(f"GET {BARS_PATH}: {exc}") from exc
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise BarsReadFailed(f"GET {BARS_PATH}: the response is not JSON") from exc
        if not isinstance(data, dict):
            raise BarsReadFailed(f"GET {BARS_PATH}: expected an object, got {type(data).__name__}")
        return data


def data_reader(env: Mapping[str, str], opener: Callable[..., Any] = urllib.request.urlopen) -> DataReader:
    """A reader on the market-data URL, built only from paper keys."""
    api_key, secret_key = paper_keys(env)
    return DataReader(api_key, secret_key, opener=opener)


def _empty(columns: list[str] = THIRTY_MINUTE_COLUMNS, time_column: str = "timestamp") -> pd.DataFrame:
    frame = pd.DataFrame(columns=columns)
    frame[time_column] = pd.to_datetime(frame[time_column])
    return frame
