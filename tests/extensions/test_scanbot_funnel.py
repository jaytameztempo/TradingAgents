"""SCANBot step 1 runs every gate in order, counts each one, fails closed on bars, and skips only
the spread gate when quotes fail. The asset list is one read-only GET to the paper host.

Fake fetchers stand in for Alpaca; no test reaches the network.
"""

import ast
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed

from extensions.market_data import alpaca_assets, alpaca_quotes
from extensions.market_data.alpaca_bars import MARKET_TZ, AlpacaDataError, MissingAlpacaKeys
from extensions.scanbot import funnel, liquidity, universe
from extensions.scanbot.funnel import (
    NOT_APPLIED,
    PRESETS,
    SAMPLED,
    CalendarError,
    load_report,
    run_funnel,
    save_report,
)
from extensions.scripts import run_scanbot_universe as cli

AS_OF = date(2026, 9, 30)
DAYS = list(pd.bdate_range(end=pd.Timestamp(AS_OF), periods=30))
FETCHED = datetime(2026, 9, 30, 22, 0, tzinfo=UTC)  # 18:00 in New York, the as-of day
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CREATED = datetime(2026, 10, 3, 12, 5, tzinfo=UTC)
PAPER_ENV = {"ALPACA_API_KEY": "PKTESTKEY000", "ALPACA_SECRET_KEY": "secret-do-not-print"}


def _asset(symbol, name="Acme Corp Common Stock", exchange="NYSE", **overrides):
    asset = {"symbol": symbol, "name": name, "exchange": exchange, "class": "us_equity",
             "status": "active", "tradable": True, "shortable": True, "easy_to_borrow": True}
    asset.update(overrides)
    return asset


ASSETS = [
    _asset("GOOD"), _asset("WIDE"), _asset("NOQ"), _asset("NEW"), _asset("QUIET"),
    _asset("THIN"), _asset("CHEAP"), _asset("NOBAR"),
    _asset("CRYP", **{"class": "crypto"}),
    _asset("INACT", status="inactive"),
    _asset("NOTRD", tradable=False),
    _asset("OTCX", exchange="OTC"),
    _asset("SPY", "SPDR S&P 500 ETF Trust", "ARCA"),
    _asset("ABCDW", exchange="NASDAQ"),
]


def _bars(symbol, days=DAYS, volume=1_000_000.0, close=50.0):
    frame = pd.DataFrame({"symbol": symbol, "date": list(days), "close": close, "volume": volume})
    for column in ("open", "high", "low", "vwap"):
        frame[column] = close
    frame["trade_count"] = 100.0
    return frame


BARS = {
    "SPY": _bars("SPY"),
    "GOOD": _bars("GOOD"),
    "WIDE": _bars("WIDE"),
    "NOQ": _bars("NOQ"),
    "NEW": _bars("NEW", DAYS[15:]),
    "QUIET": _bars("QUIET").drop(index=28),
    "THIN": _bars("THIN", volume=100_000.0),
    "CHEAP": _bars("CHEAP", volume=600_000.0, close=10.0),
}
SPREADS = {"GOOD": (49.99, 50.01), "CHEAP": (9.995, 10.005), "WIDE": (49.8, 50.2)}  # NOQ is never quoted


class FakeBars:
    def __init__(self, error=None, bars=BARS):
        self.calls = []
        self.error = error
        self.bars = bars

    def __call__(self, symbols, start, end, *, feed, cache_dir=None):
        self.calls.append((list(symbols), start, end, feed))
        if self.error:
            raise self.error
        frames = [self.bars[s] for s in symbols if s in self.bars]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["symbol", "date", "close", "volume"])


class FakeQuotes:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, symbols, start, end, *, feed):
        self.calls.append((list(symbols), start, end, feed))
        if self.error:
            raise self.error
        rows = [(s, start, *SPREADS[s]) for s in symbols if s in SPREADS]
        return pd.DataFrame(rows, columns=["symbol", "timestamp", "bid_price", "ask_price"])


def _run(preset="DEFAULT", bars=None, quotes=None, now=NOW, fetched=FETCHED):
    return run_funnel(
        AS_OF, ASSETS, fetched, "assets.json", preset,
        bars_fetcher=bars or FakeBars(), quotes_fetcher=quotes or FakeQuotes(), created_at=CREATED, now=now,
    )


# --- the funnel ---------------------------------------------------------------------------


@pytest.mark.unit
def test_every_gate_is_counted_in_order_and_every_name_is_accounted_for():
    report = _run()

    assert [(c.stage, c.gate) for c in report.stage_counts] == [
        ("universe", "class"), ("universe", "status"), ("universe", "tradable"),
        ("universe", "exchange"), ("universe", "common_stock"),
        ("liquidity", "history"), ("liquidity", "zero_volume"), ("liquidity", "share_volume"),
        ("liquidity", "dollar_volume"), ("liquidity", "spread"),
    ]
    removed = {c.gate: c.removed for c in report.stage_counts}
    assert removed == {
        "class": 1, "status": 1, "tradable": 1, "exchange": 2, "common_stock": 1,
        "history": 2, "zero_volume": 1, "share_volume": 1, "dollar_volume": 1, "spread": 2,
    }
    for before, after in zip(report.stage_counts, report.stage_counts[1:], strict=False):
        assert after.entered == before.survived
    assert report.stage_counts[0].entered == report.universe_count == len(ASSETS)
    assert report.survivors == ["GOOD"]
    assert len(report.removed) + len(report.survivors) == len(ASSETS)


@pytest.mark.unit
def test_each_removal_names_its_gate_and_reason():
    by_symbol = {r.symbol: (r.gate, r.reason) for r in _run().removed}
    assert by_symbol["SPY"][0] == "exchange"
    assert by_symbol["ABCDW"] == ("common_stock", "Nasdaq fifth letter W marks a warrant")
    assert by_symbol["NOBAR"] == ("history", "no daily bars in the lookback")
    assert by_symbol["NEW"][0] == "history"
    assert by_symbol["QUIET"][0] == "zero_volume"
    assert by_symbol["THIN"][0] == "share_volume"
    assert by_symbol["CHEAP"][0] == "dollar_volume"
    assert by_symbol["WIDE"][1].startswith("median spread 0.800% over 15 windows")
    assert by_symbol["NOQ"] == ("spread", "no quotes in the sampled windows")


@pytest.mark.unit
def test_survivor_metrics_and_run_details_are_recorded():
    report = _run()
    assert report.survivor_metrics["GOOD"] == {
        "avg_share_volume_20": 1_000_000.0, "avg_dollar_volume_20": 50_000_000.0,
        "median_spread_pct": 0.04, "spread_windows": 15,
    }
    assert report.feed == "sip" and report.spread_method == SAMPLED and report.spread_error is None
    assert report.thresholds == {"min_dollar_volume_20": 20_000_000, "min_share_volume_20": 500_000,
                                 "max_spread_pct": 0.15}
    assert report.sessions[0] == DAYS[10].date().isoformat() and report.sessions[-1] == "2026-09-30"
    assert report.spread_sampling["sessions"] == [d.date().isoformat() for d in DAYS[-5:]]
    assert report.spread_sampling["times_new_york"] == ["10:30", "12:30", "15:30"]
    assert report.point_in_time and report.warnings == []


@pytest.mark.unit
def test_bars_use_sip_with_the_calendar_first():
    bars = FakeBars()
    _run(bars=bars)
    assert bars.calls[0][0] == ["SPY"]
    assert {call[3] for call in bars.calls} == {"sip"}
    assert all(call[2] == AS_OF and call[1] == AS_OF - timedelta(days=45) for call in bars.calls)
    assert "SPY" not in bars.calls[1][0]  # SPY is the calendar, not a universe name


@pytest.mark.unit
def test_quotes_are_sampled_only_for_volume_survivors_on_sip():
    quotes = FakeQuotes()
    _run(quotes=quotes)

    assert len(quotes.calls) == 15  # 5 sessions x 3 sample times
    assert {tuple(call[0]) for call in quotes.calls} == {("GOOD", "WIDE", "NOQ")}
    assert {call[3] for call in quotes.calls} == {"sip"}
    first_start, first_end = quotes.calls[0][1], quotes.calls[0][2]
    assert first_start == MARKET_TZ.localize(datetime.combine(DAYS[-5].date(), datetime.min.time().replace(hour=10, minute=30)))
    assert first_end - first_start == timedelta(seconds=10)


@pytest.mark.unit
def test_a_refused_sip_feed_raises_before_the_universe_fetch():
    bars = FakeBars(error=AlpacaDataError("Alpaca bars request failed (403): subscription does not permit SIP"))
    with pytest.raises(AlpacaDataError, match="SIP"):
        _run(bars=bars)
    assert len(bars.calls) == 1


@pytest.mark.unit
def test_a_short_calendar_raises():
    bars = FakeBars(bars={**BARS, "SPY": _bars("SPY", DAYS[-10:])})
    with pytest.raises(CalendarError, match="only 10 sessions"):
        _run(bars=bars)


@pytest.mark.unit
@pytest.mark.parametrize("error", [
    AlpacaDataError("Alpaca quotes request failed (403): forbidden"),
    requests.exceptions.SSLError("certificate verify failed"),
])
def test_a_quote_failure_skips_only_the_spread_gate(error):
    report = _run(quotes=FakeQuotes(error=error))

    assert report.spread_method == NOT_APPLIED
    assert type(error).__name__ in report.spread_error
    spread = report.stage_counts[-1]
    assert (spread.gate, spread.applied, spread.removed) == ("spread", False, 0)
    assert report.survivors == ["GOOD", "WIDE", "NOQ"]
    assert report.survivor_metrics["GOOD"]["median_spread_pct"] is None
    assert any(w.startswith("spread gate not applied") for w in report.warnings)


@pytest.mark.unit
def test_a_bug_in_quote_handling_is_not_hidden_as_not_applied():
    with pytest.raises(ZeroDivisionError):
        _run(quotes=FakeQuotes(error=ZeroDivisionError("bug")))


@pytest.mark.unit
def test_windows_that_have_not_closed_are_not_requested():
    quotes = FakeQuotes()
    now = MARKET_TZ.localize(datetime(2026, 9, 30, 13, 0)).astimezone(UTC)
    report = _run(quotes=quotes, now=now)
    assert len(quotes.calls) == 14  # the 15:30 window on the as-of day is still open
    assert report.spread_sampling["windows_requested"] == 14


@pytest.mark.unit
def test_no_closed_window_means_spread_not_applied():
    now = MARKET_TZ.localize(datetime(2026, 9, 24, 9, 0)).astimezone(UTC)
    report = _run(now=now)
    assert report.spread_method == NOT_APPLIED
    assert "no sampling window has closed" in report.spread_error


@pytest.mark.unit
def test_presets_change_only_the_liquidity_floors():
    assert PRESETS["AGGRESSIVE"].min_dollar_volume_20 == 5_000_000
    assert PRESETS["AGGRESSIVE"].max_spread_pct == 0.30
    assert PRESETS["CONSERVATIVE"].min_dollar_volume_20 == 50_000_000
    assert PRESETS["CONSERVATIVE"].max_spread_pct == 0.08
    assert {p.min_share_volume_20 for p in PRESETS.values()} == {500_000}

    assert _run("AGGRESSIVE").survivors == ["GOOD", "CHEAP"]  # CHEAP's $6M passes; WIDE's 0.8% does not
    assert _run("CONSERVATIVE").survivors == ["GOOD"]


@pytest.mark.unit
def test_an_unknown_preset_is_refused():
    with pytest.raises(ValueError, match="unknown preset"):
        _run("RECKLESS")


@pytest.mark.unit
def test_an_asset_list_fetched_after_the_as_of_date_is_flagged():
    report = _run(fetched=datetime(2026, 10, 3, 12, 0, tzinfo=UTC))
    assert report.point_in_time is False
    assert any("fetched 2026-10-03" in w and "delisted" in w for w in report.warnings)


@pytest.mark.unit
def test_report_round_trips_and_is_never_overwritten(tmp_path):
    report = _run()
    path = save_report(report, tmp_path)

    assert path.name == "universe_2026-09-30_20261003T120500Z.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["stage_counts"][0] == {"stage": "universe", "gate": "class", "entered": 14, "removed": 1,
                                       "survived": 13, "applied": True}
    assert load_report(path) == report
    with pytest.raises(FileExistsError):
        save_report(report, tmp_path)


# --- the asset list: one read-only GET to the paper host ------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response or FakeResponse(body=[_asset("AAPL")])
        self.error = error

    def get(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        if self.error:
            raise self.error
        return self.response


@pytest.mark.unit
def test_assets_are_one_get_to_the_paper_host_with_paper_keys():
    session = FakeSession()
    assets = alpaca_assets.fetch_assets(PAPER_ENV, session=session)

    assert assets == [_asset("AAPL")]
    ((url, headers, timeout),) = session.calls
    assert url == "https://paper-api.alpaca.markets/v2/assets"
    assert headers["APCA-API-KEY-ID"] == "PKTESTKEY000"
    assert timeout == 60


@pytest.mark.unit
def test_a_live_key_is_refused_without_echoing_it():
    env = {"ALPACA_API_KEY": "AKLIVEKEY123", "ALPACA_SECRET_KEY": "live-secret"}
    with pytest.raises(MissingAlpacaKeys, match="not a paper key") as caught:
        alpaca_assets.fetch_assets(env, session=FakeSession())
    assert "AKLIVEKEY123" not in str(caught.value) and "live-secret" not in str(caught.value)


@pytest.mark.unit
@pytest.mark.parametrize("session, match", [
    (FakeSession(FakeResponse(403, text="forbidden")), r"\(403\): forbidden"),
    (FakeSession(FakeResponse(200, body=None)), "not JSON"),
    (FakeSession(FakeResponse(200, body={"assets": []})), "not a list"),
    (FakeSession(error=requests.exceptions.SSLError("certificate verify failed")), "SSLError"),
])
def test_asset_failures_are_data_errors(session, match):
    with pytest.raises(AlpacaDataError, match=match):
        alpaca_assets.fetch_assets(PAPER_ENV, session=session)


@pytest.mark.unit
def test_asset_snapshot_round_trips_and_is_never_overwritten(tmp_path):
    path = alpaca_assets.save_snapshot([_asset("AAPL")], tmp_path, fetched_at=FETCHED)
    assert path.name == "assets_20260930T220000Z.json"
    assert alpaca_assets.load_snapshot(path) == (FETCHED, [_asset("AAPL")])
    with pytest.raises(FileExistsError):
        alpaca_assets.save_snapshot([_asset("AAPL")], tmp_path, fetched_at=FETCHED)


def _imports_and_names(module):
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported, names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return imported, names


@pytest.mark.unit
def test_the_asset_module_imports_no_alpaca_package_and_can_only_get():
    imported, names = _imports_and_names(alpaca_assets)
    assert not any(m == "alpaca" or m.startswith("alpaca.") for m in imported)
    assert not names & {"post", "put", "patch", "delete", "request", "Session"}
    source = Path(alpaca_assets.__file__).read_text(encoding="utf-8")
    assert "orders" not in source and "positions" not in source


@pytest.mark.unit
def test_scanbot_code_never_names_the_trading_client_or_orders():
    for module in (alpaca_assets, alpaca_quotes, universe, liquidity, funnel, cli):
        imported, names = _imports_and_names(module)
        assert not any(m.startswith("alpaca.trading") for m in imported), module.__name__
        assert not {n for n in names if "order" in n.lower() or n == "TradingClient"}, module.__name__


# --- quotes --------------------------------------------------------------------------------


class _QuoteSet:
    def __init__(self, df):
        self.df = df


class FakeQuoteClient:
    def __init__(self, error=None):
        self.requests = []
        self.error = error

    def get_stock_quotes(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        index = pd.MultiIndex.from_tuples(
            [("AAPL", pd.Timestamp("2026-09-30 14:30:01", tz="UTC"))], names=["symbol", "timestamp"]
        )
        return _QuoteSet(pd.DataFrame({"bid_price": [99.99], "ask_price": [100.01], "bid_size": [1.0]}, index=index))


WINDOW = (datetime(2026, 9, 30, 14, 30, tzinfo=UTC), datetime(2026, 9, 30, 14, 30, 10, tzinfo=UTC))


@pytest.mark.unit
def test_quotes_are_requested_on_sip_and_flattened():
    client = FakeQuoteClient()
    quotes = alpaca_quotes.fetch_quotes(["aapl"], *WINDOW, client=client)

    assert list(quotes.columns) == ["symbol", "timestamp", "bid_price", "ask_price"]
    assert quotes.loc[0, "symbol"] == "AAPL"
    (request,) = client.requests
    assert request.feed == DataFeed.SIP and request.symbol_or_symbols == ["AAPL"]


@pytest.mark.unit
def test_quote_errors_are_data_errors():
    with pytest.raises(AlpacaDataError):
        alpaca_quotes.fetch_quotes(["AAPL"], *WINDOW, client=FakeQuoteClient(APIError('{"message": "no"}')))


@pytest.mark.unit
def test_quote_windows_must_be_timezone_aware():
    with pytest.raises(ValueError, match="timezone-aware"):
        alpaca_quotes.fetch_quotes(["AAPL"], datetime(2026, 9, 30, 10), datetime(2026, 9, 30, 11), client=FakeQuoteClient())


# --- the command line ----------------------------------------------------------------------


@pytest.fixture
def fake_funnel(monkeypatch):
    """The CLI's run_funnel, with fake fetchers in place of Alpaca."""
    state = {"bars": FakeBars(), "quotes": FakeQuotes()}
    real = funnel.run_funnel

    def run(*args, **kwargs):
        return real(*args, bars_fetcher=state["bars"], quotes_fetcher=state["quotes"], now=NOW,
                    **{k: v for k, v in kwargs.items() if k != "cache_dir"})

    monkeypatch.setattr(funnel, "run_funnel", run)
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    return state


def _snapshot(tmp_path):
    return alpaca_assets.save_snapshot(ASSETS, tmp_path / "assets", fetched_at=FETCHED)


@pytest.mark.unit
def test_cli_prints_every_gate_and_writes_the_report(tmp_path, fake_funnel, capsys):
    snapshot = _snapshot(tmp_path)

    code = cli.main(["--as-of", "2026-09-30", "--assets-file", str(snapshot), "--out-dir", str(tmp_path / "out")])

    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "14 asset records in" in out
    assert "universe/exchange        removed      2   survived      9" in out
    assert "liquidity/spread         removed      2   survived      1" in out
    assert "survivors: 1  (spread_method sampled)" in out
    (path,) = (tmp_path / "out").glob("universe_*.json")
    assert load_report(path).survivors == ["GOOD"]


@pytest.mark.unit
def test_cli_writes_no_report_when_sip_is_refused(tmp_path, fake_funnel, capsys):
    fake_funnel["bars"] = FakeBars(error=AlpacaDataError("Alpaca bars request failed (403): SIP not permitted"))

    code = cli.main(["--as-of", "2026-09-30", "--assets-file", str(_snapshot(tmp_path)), "--out-dir", str(tmp_path / "out")])

    assert code == cli.EXIT_FAILED
    assert "SIP not permitted; no report written" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


@pytest.mark.unit
def test_cli_writes_no_report_on_a_network_error_for_bars(tmp_path, fake_funnel, capsys):
    fake_funnel["bars"] = FakeBars(error=requests.exceptions.SSLError("certificate verify failed"))

    code = cli.main(["--as-of", "2026-09-30", "--assets-file", str(_snapshot(tmp_path)), "--out-dir", str(tmp_path / "out")])

    assert code == cli.EXIT_FAILED
    assert "certificate verify failed; no report written" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


@pytest.mark.unit
def test_cli_writes_the_report_with_spread_not_applied_when_quotes_fail(tmp_path, fake_funnel, capsys):
    fake_funnel["quotes"] = FakeQuotes(error=AlpacaDataError("Alpaca quotes request failed (500)"))

    code = cli.main(["--as-of", "2026-09-30", "--assets-file", str(_snapshot(tmp_path)), "--out-dir", str(tmp_path / "out")])

    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "liquidity/spread         NOT APPLIED" in out
    assert "warning: spread gate not applied" in out
    (path,) = (tmp_path / "out").glob("universe_*.json")
    assert load_report(path).spread_method == NOT_APPLIED


@pytest.mark.unit
def test_cli_fetches_and_saves_an_asset_snapshot(tmp_path, fake_funnel, monkeypatch, capsys):
    monkeypatch.setattr(alpaca_assets, "fetch_assets", lambda: ASSETS)

    code = cli.main(["--as-of", "2026-09-30", "--assets-dir", str(tmp_path / "assets"), "--out-dir", str(tmp_path / "out")])

    assert code == cli.EXIT_OK
    (snapshot,) = (tmp_path / "assets").glob("assets_*.json")
    assert f"asset snapshot: 14 records saved to {snapshot}" in capsys.readouterr().out
    (path,) = (tmp_path / "out").glob("universe_*.json")
    assert load_report(path).assets_source == str(snapshot)


@pytest.mark.unit
def test_cli_writes_nothing_when_the_asset_fetch_fails(tmp_path, fake_funnel, monkeypatch, capsys):
    def refuse():
        raise AlpacaDataError("Alpaca assets request failed (401): unauthorized")

    monkeypatch.setattr(alpaca_assets, "fetch_assets", refuse)

    code = cli.main(["--as-of", "2026-09-30", "--assets-dir", str(tmp_path / "assets"), "--out-dir", str(tmp_path / "out")])

    assert code == cli.EXIT_FAILED
    assert "no report written" in capsys.readouterr().err
    assert not (tmp_path / "assets").exists() and not (tmp_path / "out").exists()


@pytest.mark.unit
@pytest.mark.parametrize("argv", [
    ["--as-of", "30/09/2026"],
    ["--as-of", (date.today() + timedelta(days=30)).isoformat()],
    ["--as-of", "2026-09-30", "--assets-file", "does-not-exist.json"],
])
def test_cli_refuses_bad_input(argv, fake_funnel):
    assert cli.main(argv) == cli.EXIT_BAD_INPUT
