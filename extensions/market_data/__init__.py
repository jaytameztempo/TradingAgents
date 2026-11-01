"""Read-only market data. Nothing in this package can place an order."""

from extensions.tls import use_system_trust_store

use_system_trust_store()  # before any Alpaca request; see extensions/tls.py

from extensions.market_data.alpaca_bars import (  # noqa: E402
    AlpacaDataError,
    MissingAlpacaKeys,
    fetch_daily_bars,
    make_client,
    validate_date_range,
    validate_ticker,
)
