"""Historical quotes from Alpaca's market-data API, read-only, for sampled spread checks.

Like the bar fetcher, this builds only the market-data client, which has no
order endpoints. Quotes are requested for short windows only: a full day of
quotes for a liquid name runs to millions of rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockQuotesRequest

from extensions.market_data.alpaca_bars import AlpacaDataError, make_client, validate_ticker

QUOTE_COLUMNS = ["symbol", "timestamp", "bid_price", "ask_price"]


def fetch_quotes(
    symbols: Iterable[str],
    start: datetime,
    end: datetime,
    *,
    client: StockHistoricalDataClient | None = None,
    feed: DataFeed | str = DataFeed.SIP,
) -> pd.DataFrame:
    """Every quote for the symbols from start (inclusive) to end, columns QUOTE_COLUMNS.

    start and end must be timezone-aware. A symbol with no quote in the window is simply absent.
    """
    tickers = list(dict.fromkeys(validate_ticker(s) for s in symbols))
    if not tickers:
        raise ValueError("no tickers given")
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    if start >= end:
        raise ValueError(f"start {start} is not before end {end}")

    client = make_client() if client is None else client
    request = StockQuotesRequest(symbol_or_symbols=tickers, start=start, end=end, feed=DataFeed(feed))
    try:
        quotes = client.get_stock_quotes(request)
    except APIError as exc:
        raise AlpacaDataError(f"Alpaca quotes request failed ({exc.status_code}): {exc}") from exc

    raw = quotes.df
    if raw.empty:
        return pd.DataFrame(columns=QUOTE_COLUMNS)
    return raw.reset_index().reindex(columns=QUOTE_COLUMNS)
