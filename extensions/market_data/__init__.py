"""Read-only market data. Nothing in this package can place an order."""

from extensions.market_data.alpaca_bars import (
    AlpacaDataError,
    MissingAlpacaKeys,
    fetch_daily_bars,
    make_client,
    validate_date_range,
    validate_ticker,
)
