"""SCANBot's universe gates keep active, tradable, listed common stock and say why every other record left.

Asset records here are made up in Alpaca's shape; nothing calls Alpaca.
"""

import pytest

from extensions.scanbot.universe import (
    UNIVERSE_GATES,
    gate_class,
    gate_common_stock,
    gate_exchange,
    gate_status,
    gate_tradable,
    non_common_reason,
    universe_symbol,
)


def _asset(symbol="AAPL", name="Apple Inc. Common Stock", exchange="NASDAQ", **overrides):
    asset = {
        "symbol": symbol, "name": name, "exchange": exchange, "class": "us_equity", "status": "active",
        "tradable": True, "shortable": True, "easy_to_borrow": True,
    }
    asset.update(overrides)
    return asset


@pytest.mark.unit
@pytest.mark.parametrize("gate, field, bad", [
    (gate_class, "class", "crypto"),
    (gate_status, "status", "inactive"),
    (gate_tradable, "tradable", False),
])
def test_field_gates_remove_and_explain(gate, field, bad):
    kept, removed = gate([_asset("AAPL"), _asset("MSFT", **{field: bad})])
    assert [a["symbol"] for a in kept] == ["AAPL"]
    (removal,) = removed
    assert removal.symbol == "MSFT"
    assert removal.reason.startswith(f"{field} is {bad!r}")


@pytest.mark.unit
@pytest.mark.parametrize("exchange", ["OTC", "ARCA", "BATS", None])
def test_only_nyse_nasdaq_and_amex_listings_pass(exchange):
    kept, removed = gate_exchange([_asset("AAPL"), _asset("XYZ", exchange=exchange)])
    assert [a["symbol"] for a in kept] == ["AAPL"]
    assert removed[0].gate == "exchange" and "needs one of NYSE, NASDAQ, AMEX" in removed[0].reason


@pytest.mark.unit
@pytest.mark.parametrize("symbol, name, exchange", [
    ("AAPL", "Apple Inc. Common Stock", "NASDAQ"),
    ("BRK.B", "Berkshire Hathaway Inc. Class B", "NYSE"),
    ("BF-B", "Brown-Forman Corporation Class B", "NYSE"),
    ("GOOGL", "Alphabet Inc. Class A", "NASDAQ"),
    ("UCTT", "Ultra Clean Holdings, Inc. Common Stock", "NASDAQ"),
    ("IVZ", "Invesco Ltd", "NYSE"),
    ("FGF", "Fundamental Global Inc. Common Stock", "NASDAQ"),
    ("TSM", "Taiwan Semiconductor Manufacturing Company Ltd. American Depositary Shares", "NYSE"),
])
def test_common_stock_passes(symbol, name, exchange):
    assert non_common_reason(_asset(symbol, name, exchange)) is None


@pytest.mark.unit
@pytest.mark.parametrize("symbol, name, exchange, label", [
    ("VOO", "Vanguard S&P 500 ETF", "NYSE", "ETF"),
    ("XLK", "Technology Select Sector SPDR Fund", "NYSE", "fund"),
    ("QQQ", "Invesco QQQ Trust, Series 1", "NASDAQ", "unit trust"),
    ("IVV", "iShares Core S&P 500", "NYSE", "fund family"),
    ("TQQQ", "ProShares UltraPro QQQ", "NASDAQ", "fund family"),
    ("SOXS", "Semiconductor Bear 3X Shares", "AMEX", "leveraged or inverse"),
    ("SQQQ", "Inverse Nasdaq Tracker", "NASDAQ", "leveraged or inverse"),
    ("VXX", "iPath Series B S&P 500 VIX Short-Term Futures ETN", "NYSE", "ETN"),
    ("ACME", "Acme Corp Warrants", "NYSE", "warrant"),
    ("ACMR", "Acme Corp Rights", "NYSE", "right"),
    ("ACMU", "Acme Acquisition Units", "NYSE", "unit"),
    ("ACMP", "Acme Corp 7% Series A Preferred Stock", "NYSE", "preferred"),
    ("ACMN", "Acme Corp 6.25% Notes due 2030", "NYSE", "note or debenture"),
    ("ACMC", "Acme Corp 5.5% Fixed Rate Securities", "NYSE", "fixed coupon"),
])
def test_non_common_names_are_recognised(symbol, name, exchange, label):
    assert non_common_reason(_asset(symbol, name, exchange)) == f"name matches {label}: {name!r}"


@pytest.mark.unit
@pytest.mark.parametrize("symbol, exchange, expected", [
    ("ACME.WS", "NYSE", "symbol suffix WS marks a warrant"),
    ("ACME.U", "NYSE", "symbol suffix U marks a unit"),
    ("ACME.RT", "NYSE", "symbol suffix RT marks a right"),
    ("BAC.PL", "NYSE", "symbol suffix PL marks a preferred"),
    ("C-PN", "NYSE", "symbol suffix PN marks a preferred"),
    ("ABCDW", "NASDAQ", "Nasdaq fifth letter W marks a warrant"),
    ("ABCDR", "NASDAQ", "Nasdaq fifth letter R marks a right"),
    ("ABCDU", "NASDAQ", "Nasdaq fifth letter U marks a unit"),
])
def test_non_common_symbols_are_recognised(symbol, exchange, expected):
    assert non_common_reason(_asset(symbol, "Acme Corp Common Stock", exchange)) == expected


@pytest.mark.unit
def test_a_five_letter_nyse_symbol_is_not_read_as_a_nasdaq_suffix():
    assert non_common_reason(_asset("ABCDW", "Acme Corp Common Stock", "NYSE")) is None


@pytest.mark.unit
@pytest.mark.parametrize("symbol", ["BAC.PR.A", "TOOLONG", "", None, "AB/C"])
def test_a_symbol_that_is_not_a_plain_ticker_is_removed(symbol):
    assert "is not a plain ticker" in non_common_reason(_asset(symbol))


@pytest.mark.unit
def test_a_duplicate_listing_is_kept_once():
    kept, removed = gate_common_stock([_asset("AAPL"), _asset("aapl")])
    assert len(kept) == 1
    assert removed[0].reason == "duplicate listing of the symbol"


@pytest.mark.unit
def test_borrow_flags_are_not_a_universe_gate():
    asset = _asset("AAPL", shortable=False, easy_to_borrow=False)
    records = [asset]
    for _, gate in UNIVERSE_GATES:
        records, _ = gate(records)
    assert records == [asset]


@pytest.mark.unit
def test_the_gates_run_in_spec_order_and_account_for_every_record():
    assets = [
        _asset("AAPL"),
        _asset("BTCX", **{"class": "crypto"}),
        _asset("OLD", status="inactive"),
        _asset("HALT", tradable=False),
        _asset("PINK", exchange="OTC"),
        _asset("SPY", "SPDR S&P 500 ETF Trust", "ARCA"),
        _asset("VOO", "Vanguard S&P 500 ETF", "NYSE"),
    ]
    assert [name for name, _ in UNIVERSE_GATES] == ["class", "status", "tradable", "exchange", "common_stock"]

    records, removed_by = assets, {}
    for name, gate in UNIVERSE_GATES:
        records, removed = gate(records)
        removed_by[name] = [r.symbol for r in removed]

    assert [universe_symbol(a) for a in records] == ["AAPL"]
    assert removed_by == {
        "class": ["BTCX"], "status": ["OLD"], "tradable": ["HALT"],
        "exchange": ["PINK", "SPY"], "common_stock": ["VOO"],
    }
