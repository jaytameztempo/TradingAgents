"""SCAN-Upward Trend Momentum keeps only names that pass all three checks, and records why others failed.

Bars here are made up; no test reaches Alpaca.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from extensions.market_data.alpaca_bars import FRAME_COLUMNS
from extensions.scans import upward_trend_momentum as scan
from extensions.scans.basket import default_basket_dir, load_basket, save_basket
from extensions.scans.upward_trend_momentum import build_basket, check_uptrend, run_scan
from extensions.scripts import run_upward_trend_scan as cli

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)


def _bars(symbol, closes, end=AS_OF):
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    frame = pd.DataFrame({"symbol": symbol, "date": dates, "close": list(closes)})
    for column in ("open", "high", "low"):
        frame[column] = frame["close"]
    frame["volume"], frame["trade_count"], frame["vwap"] = 1_000.0, 10.0, frame["close"]
    return frame[FRAME_COLUMNS]


RISING = [100 + i * 0.5 for i in range(260)]
FALLING = [230 - i * 0.5 for i in range(260)]
# 50-day above the 200-day, but the last close dips below the 50-day.
DIP = [100.0] * 200 + [110.0] * 49 + [105.0]
# Last close above the 50-day, but the 50-day is still below the 200-day.
BOUNCE = [100.0] * 200 + [90.0] * 49 + [95.0]


@pytest.mark.unit
def test_a_steady_uptrend_passes():
    result = check_uptrend(pd.Series(RISING))
    assert result.passed
    assert result.reasons == []
    assert result.close > result.sma_fast > result.sma_slow


@pytest.mark.unit
def test_fewer_than_200_bars_fails_on_history():
    result = check_uptrend(pd.Series(RISING[:199]))
    assert not result.passed
    assert result.reasons == ["only 199 daily bars; needs 200"]


@pytest.mark.unit
def test_exactly_200_bars_is_enough_history():
    assert check_uptrend(pd.Series(RISING[:200])).passed


@pytest.mark.unit
def test_close_below_the_50_day_fails():
    result = check_uptrend(pd.Series(DIP))
    assert not result.passed
    assert len(result.reasons) == 1
    assert "not above the 50-day average" in result.reasons[0]


@pytest.mark.unit
def test_50_day_below_the_200_day_fails():
    result = check_uptrend(pd.Series(BOUNCE))
    assert not result.passed
    assert len(result.reasons) == 1
    assert "50-day average" in result.reasons[0] and "200-day average" in result.reasons[0]


@pytest.mark.unit
def test_a_downtrend_fails_both_average_checks():
    assert len(check_uptrend(pd.Series(FALLING)).reasons) == 2


@pytest.mark.unit
def test_a_flat_series_is_not_above_its_averages():
    assert not check_uptrend(pd.Series([100.0] * 250)).passed


@pytest.mark.unit
def test_basket_keeps_passers_ranked_and_explains_every_rejection():
    bars = pd.concat([
        _bars("SPY", RISING),
        _bars("QQQ", [100 + i * 1.0 for i in range(260)]),
        _bars("AAPL", FALLING),
        _bars("MSFT", RISING[:120]),
    ])

    basket = build_basket(bars, ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"], AS_OF, created_at=CREATED)

    assert basket.scan_name == "SCAN-Upward Trend Momentum"
    assert basket.tickers == ["QQQ", "SPY"]  # QQQ is further above its 50-day
    assert basket.members[0].score > basket.members[1].score > 0
    reasons = {r.ticker: r.reason for r in basket.rejected}
    assert reasons["NVDA"] == "no bars"
    assert "not above" in reasons["AAPL"]
    assert reasons["MSFT"] == "only 120 daily bars; needs 200"
    assert set(basket.tickers) | set(reasons) == set(basket.universe)


@pytest.mark.unit
def test_bars_after_the_scan_date_are_ignored():
    """A crash dated after the scan date must not change a past scan's answer."""
    later = AS_OF + timedelta(days=14)
    closes = RISING + [10.0] * 10  # ten business days of collapse, all after AS_OF
    bars = _bars("SPY", closes, end=later)
    assert bars.loc[bars["date"] <= pd.Timestamp(AS_OF), "close"].iloc[-1] > 10

    basket = build_basket(bars, ["SPY"], AS_OF, created_at=CREATED)

    assert basket.tickers == ["SPY"]
    assert basket.members[0].metrics["last_bar_date"] <= AS_OF.isoformat()


@pytest.mark.unit
def test_run_scan_fetches_a_lookback_ending_on_the_scan_date(monkeypatch):
    calls = []

    def fake_fetch(symbols, start, end, **kwargs):
        calls.append((symbols, start, end))
        return _bars("SPY", RISING)

    monkeypatch.setattr(scan, "fetch_daily_bars", fake_fetch)

    basket = run_scan(AS_OF, ["spy", "QQQ"])

    assert calls == [(["SPY", "QQQ"], AS_OF - timedelta(days=400), AS_OF)]
    assert basket.tickers == ["SPY"]
    assert [r.ticker for r in basket.rejected] == ["QQQ"]


@pytest.mark.unit
def test_basket_round_trips_through_json(tmp_path):
    basket = build_basket(_bars("SPY", RISING), ["SPY", "QQQ"], AS_OF, created_at=CREATED)

    path = save_basket(basket, tmp_path)

    assert path.name == "scan-upward-trend-momentum_2026-09-30_20260930T210000Z.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["scan_name"] == "SCAN-Upward Trend Momentum"
    assert data["as_of"] == "2026-09-30"
    assert data["members"][0]["ticker"] == "SPY"
    assert load_basket(path) == basket


@pytest.mark.unit
def test_an_existing_basket_file_is_never_overwritten(tmp_path):
    basket = build_basket(_bars("SPY", RISING), ["SPY"], AS_OF, created_at=CREATED)
    save_basket(basket, tmp_path)
    with pytest.raises(FileExistsError):
        save_basket(basket, tmp_path)


@pytest.mark.unit
def test_default_basket_dir_is_under_dot_tradingagents(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_basket_dir() == tmp_path / ".tradingagents" / "baskets"


def _fake_run(monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(
        scan, "fetch_daily_bars",
        lambda symbols, start, end, **k: pd.concat([_bars("SPY", RISING), _bars("AAPL", FALLING)]),
    )


@pytest.mark.unit
def test_cli_writes_the_basket_to_out_dir(monkeypatch, tmp_path, capsys):
    _fake_run(monkeypatch)

    code = cli.main(["--as-of", "2026-09-30", "--tickers", "SPY", "AAPL", "--out-dir", str(tmp_path)])

    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "1 of 2 passed" in out and "PASS SPY" in out and "FAIL AAPL" in out
    (path,) = tmp_path.glob("*.json")
    assert load_basket(path).tickers == ["SPY"]


@pytest.mark.unit
def test_cli_writes_to_the_default_folder_without_out_dir(monkeypatch, tmp_path):
    _fake_run(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert cli.main(["--as-of", "2026-09-30", "--tickers", "SPY"]) == cli.EXIT_OK

    assert len(list((tmp_path / ".tradingagents" / "baskets").glob("*.json"))) == 1


@pytest.mark.unit
@pytest.mark.parametrize("argv", [
    ["--as-of", "2026-09-30", "--tickers", "SPY;rm"],
    ["--as-of", "30/09/2026"],
    ["--as-of", (date.today() + timedelta(days=30)).isoformat()],
])
def test_cli_refuses_bad_input_before_fetching(monkeypatch, argv):
    monkeypatch.setattr(scan, "fetch_daily_bars", lambda *a, **k: pytest.fail("fetched"))
    assert cli.main(argv) == cli.EXIT_BAD_INPUT


@pytest.mark.unit
def test_cli_reports_missing_keys_without_writing_a_basket(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    code = cli.main(["--as-of", "2026-09-30", "--out-dir", str(tmp_path)])

    assert code == cli.EXIT_FAILED
    assert "ALPACA_API_KEY" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
