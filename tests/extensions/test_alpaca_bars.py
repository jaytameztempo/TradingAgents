"""The Alpaca bar fetcher reads prices only, checks its inputs, and keeps keys out of messages.

No test reaches Alpaca: a fake client stands in for the market-data API.
"""

import ast
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.timeframe import TimeFrame

from extensions.market_data import alpaca_bars
from extensions.market_data.alpaca_bars import (
    FRAME_COLUMNS,
    AlpacaDataError,
    MissingAlpacaKeys,
    fetch_daily_bars,
    make_client,
    validate_date_range,
    validate_ticker,
)
from extensions.scripts import fetch_bars

EXTENSIONS = Path(alpaca_bars.__file__).resolve().parents[1]
PAPER_ENV = {"ALPACA_API_KEY": "PKTESTKEY000", "ALPACA_SECRET_KEY": "secret-do-not-print"}


class _Bars:
    def __init__(self, df):
        self.df = df


class FakeClient:
    """Answers get_stock_bars with two daily bars per requested symbol, the way Alpaca shapes them."""

    def __init__(self, error=None):
        self.requests = []
        self.error = error

    def get_stock_bars(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        symbols = request.symbol_or_symbols
        # Alpaca stamps a daily bar at midnight New York time, given in UTC.
        stamps = pd.to_datetime(["2026-06-01 04:00", "2026-06-02 04:00"], utc=True)
        index = pd.MultiIndex.from_product([symbols, stamps], names=["symbol", "timestamp"])
        n = len(index)
        df = pd.DataFrame(
            {
                "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n, "close": [100.5] * n,
                "volume": [1_000.0] * n, "trade_count": [10.0] * n, "vwap": [100.2] * n,
            },
            index=index,
        )
        return _Bars(df)


@pytest.mark.unit
@pytest.mark.parametrize("raw, expected", [("SPY", "SPY"), (" qqq ", "QQQ"), ("brk.b", "BRK.B"), ("BF-B", "BF-B")])
def test_valid_tickers_are_normalized(raw, expected):
    assert validate_ticker(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "TOOLONG", "SPY;DROP", "../etc", "12AB", "SP Y", None])
def test_invalid_tickers_are_refused(raw):
    with pytest.raises(ValueError):
        validate_ticker(raw)


@pytest.mark.unit
def test_date_range_parses_iso_dates():
    assert validate_date_range("2026-06-01", "2026-06-30") == (date(2026, 6, 1), date(2026, 6, 30))


@pytest.mark.unit
@pytest.mark.parametrize("start, end", [("2026-06-30", "2026-06-01"), ("2026-13-01", "2026-06-01"), ("June 1", "2026-06-02")])
def test_bad_date_ranges_are_refused(start, end):
    with pytest.raises(ValueError):
        validate_date_range(start, end)


@pytest.mark.unit
def test_an_end_date_after_today_is_refused():
    """A bar dated after today cannot exist; in a backtest, asking for one is lookahead."""
    tomorrow = alpaca_bars.market_today() + timedelta(days=1)
    with pytest.raises(ValueError, match="after today"):
        validate_date_range("2026-01-02", tomorrow)


@pytest.mark.unit
@pytest.mark.parametrize("env", [{}, {"ALPACA_API_KEY": "PKX"}, {"ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": " "}])
def test_missing_keys_name_the_variables(env):
    with pytest.raises(MissingAlpacaKeys, match="ALPACA_"):
        make_client(env)


@pytest.mark.unit
def test_a_live_key_is_refused_without_echoing_it():
    env = {"ALPACA_API_KEY": "AKLIVEKEY123", "ALPACA_SECRET_KEY": "live-secret"}
    with pytest.raises(MissingAlpacaKeys, match="not a paper key") as caught:
        make_client(env)
    assert "AKLIVEKEY123" not in str(caught.value)
    assert "live-secret" not in str(caught.value)


@pytest.mark.unit
def test_paper_keys_build_the_market_data_client():
    assert isinstance(make_client(PAPER_ENV), StockHistoricalDataClient)


@pytest.mark.unit
def test_fetch_returns_one_row_per_symbol_and_trading_date():
    client = FakeClient()

    bars = fetch_daily_bars(["spy", "QQQ", "SPY"], "2026-06-01", "2026-06-02", client=client)

    assert list(bars.columns) == FRAME_COLUMNS
    assert bars["symbol"].tolist() == ["QQQ", "QQQ", "SPY", "SPY"]
    assert bars["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-06-01", "2026-06-02"] * 2
    (request,) = client.requests
    assert request.symbol_or_symbols == ["SPY", "QQQ"]
    assert request.timeframe.value == TimeFrame.Day.value
    assert request.feed == DataFeed.IEX
    assert request.adjustment == Adjustment.SPLIT


@pytest.mark.unit
def test_alpaca_errors_are_raised_as_data_errors():
    client = FakeClient(error=APIError('{"message": "forbidden"}'))
    with pytest.raises(AlpacaDataError):
        fetch_daily_bars("SPY", "2026-06-01", "2026-06-02", client=client)


@pytest.mark.unit
def test_a_completed_range_is_read_back_from_the_cache(tmp_path):
    client = FakeClient()
    first = fetch_daily_bars("SPY", "2026-06-01", "2026-06-02", client=client, cache_dir=tmp_path)

    second = fetch_daily_bars(["SPY", "QQQ"], "2026-06-01", "2026-06-02", client=client, cache_dir=tmp_path)

    assert [r.symbol_or_symbols for r in client.requests] == [["SPY"], ["QQQ"]]
    pd.testing.assert_frame_equal(
        second[second["symbol"] == "SPY"].reset_index(drop=True), first, check_dtype=False
    )


@pytest.mark.unit
def test_a_range_ending_today_is_not_cached(tmp_path):
    today = alpaca_bars.market_today()
    fetch_daily_bars("SPY", today - timedelta(days=5), today, client=FakeClient(), cache_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_extensions_never_import_the_trading_client():
    """Only the market-data client may appear in project code; orders come later, behind approval.

    alpaca-py's own data package loads alpaca.trading internally, so this checks
    what our code imports and names, not what ends up in sys.modules.
    """
    offenders = []
    for path in EXTENSIONS.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            if any(m.startswith("alpaca.trading") for m in modules):
                offenders.append(f"{path.name}: imports {modules}")
            if isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                if name in {"TradingClient", "submit_order", "MarketOrderRequest", "LimitOrderRequest"}:
                    offenders.append(f"{path.name}: names {name}")
    assert offenders == []


@pytest.mark.unit
def test_the_market_data_client_has_no_order_methods():
    methods = {m for m in dir(StockHistoricalDataClient) if not m.startswith("_")}
    assert not {m for m in methods if "order" in m or "position" in m}


@pytest.mark.unit
def test_cli_prints_a_summary_and_writes_csv(tmp_path, monkeypatch, capsys):
    client = FakeClient()
    real_fetch = alpaca_bars.fetch_daily_bars
    monkeypatch.setattr(fetch_bars, "fetch_daily_bars", lambda *a, **k: real_fetch(*a, client=client, **k))
    out = tmp_path / "bars.csv"

    code = fetch_bars.main(["SPY", "--start", "2026-06-01", "--end", "2026-06-02", "--out", str(out)])

    assert code == fetch_bars.EXIT_OK
    assert "SPY: 2 bars, 2026-06-01 to 2026-06-02, last close 100.50" in capsys.readouterr().out
    assert len(pd.read_csv(out)) == 2


@pytest.mark.unit
def test_cli_refuses_a_bad_ticker_before_any_request(monkeypatch, capsys):
    monkeypatch.setattr(fetch_bars, "fetch_daily_bars", lambda *a, **k: pytest.fail("fetched"))

    code = fetch_bars.main(["SPY;rm", "--start", "2026-06-01", "--end", "2026-06-02"])

    assert code == fetch_bars.EXIT_BAD_INPUT
    assert "not a valid US ticker" in capsys.readouterr().err


@pytest.mark.unit
def test_cli_reports_missing_keys_without_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(fetch_bars, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    code = fetch_bars.main(["SPY", "--start", "2026-06-01", "--end", "2026-06-02"])

    assert code == fetch_bars.EXIT_FAILED
    assert "ALPACA_API_KEY" in capsys.readouterr().err
