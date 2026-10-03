"""SCAN-Breakdown Short Candidates keeps only names that pass all three checks, and records why others failed.

Bars here are made up; no test reaches Alpaca.
"""

import ast
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from extensions.market_data.alpaca_bars import FRAME_COLUMNS
from extensions.router.strategy_router import latest_basket
from extensions.scans import breakdown_short_candidates as scan
from extensions.scans import upward_trend_momentum
from extensions.scans.basket import load_basket, save_basket
from extensions.scans.breakdown_short_candidates import build_basket, check_downtrend, run_scan
from extensions.scripts import run_breakdown_scan as cli

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)


def _bars(symbol, closes, end=AS_OF):
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    frame = pd.DataFrame({"symbol": symbol, "date": dates, "close": list(closes)})
    for column in ("open", "high", "low"):
        frame[column] = frame["close"]
    frame["volume"], frame["trade_count"], frame["vwap"] = 1_000.0, 10.0, frame["close"]
    return frame[FRAME_COLUMNS]


FALLING = [230 - i * 0.5 for i in range(260)]
RISING = [100 + i * 0.5 for i in range(260)]
# 50-day below the 200-day, but the last close bounces above the 50-day.
BOUNCE = [100.0] * 200 + [90.0] * 49 + [95.0]
# Last close below the 50-day, but the 50-day is still above the 200-day.
DIP = [100.0] * 200 + [110.0] * 49 + [105.0]


@pytest.mark.unit
def test_a_steady_downtrend_passes():
    result = check_downtrend(pd.Series(FALLING))
    assert result.passed
    assert result.reasons == []
    assert result.close < result.sma_fast < result.sma_slow


@pytest.mark.unit
def test_fewer_than_200_bars_fails_on_history():
    result = check_downtrend(pd.Series(FALLING[:199]))
    assert not result.passed
    assert result.reasons == ["only 199 daily bars; needs 200"]


@pytest.mark.unit
def test_exactly_200_bars_is_enough_history():
    assert check_downtrend(pd.Series(FALLING[:200])).passed


@pytest.mark.unit
def test_close_above_the_50_day_fails():
    result = check_downtrend(pd.Series(BOUNCE))
    assert not result.passed
    assert len(result.reasons) == 1
    assert "not below the 50-day average" in result.reasons[0]


@pytest.mark.unit
def test_50_day_above_the_200_day_fails():
    result = check_downtrend(pd.Series(DIP))
    assert not result.passed
    assert len(result.reasons) == 1
    assert "50-day average" in result.reasons[0] and "not below the 200-day average" in result.reasons[0]


@pytest.mark.unit
def test_an_uptrend_fails_both_average_checks():
    assert len(check_downtrend(pd.Series(RISING)).reasons) == 2


@pytest.mark.unit
def test_a_flat_series_is_not_strictly_below_its_averages():
    result = check_downtrend(pd.Series([100.0] * 250))
    assert not result.passed
    assert len(result.reasons) == 2


@pytest.mark.unit
def test_basket_keeps_passers_ranked_and_explains_every_rejection():
    bars = pd.concat([
        _bars("SPY", FALLING),
        _bars("QQQ", [400 - i * 1.0 for i in range(260)]),
        _bars("AAPL", RISING),
        _bars("MSFT", FALLING[:120]),
    ])

    basket = build_basket(bars, ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"], AS_OF, created_at=CREATED)

    assert basket.scan_name == "SCAN-Breakdown Short Candidates"
    assert basket.tickers == ["QQQ", "SPY"]  # QQQ is further below its 50-day
    assert basket.members[0].score > basket.members[1].score > 0
    assert basket.parameters["score"] == "1 - close / sma_50"
    reasons = {r.ticker: r.reason for r in basket.rejected}
    assert reasons["NVDA"] == "no bars"
    assert "not below" in reasons["AAPL"]
    assert reasons["MSFT"] == "only 120 daily bars; needs 200"
    assert set(basket.tickers) | set(reasons) == set(basket.universe)


@pytest.mark.unit
def test_bars_after_the_scan_date_are_ignored():
    """A rally dated after the scan date must not change a past scan's answer."""
    later = AS_OF + timedelta(days=14)
    closes = FALLING + [1_000.0] * 10  # ten business days of rally, all after AS_OF
    bars = _bars("SPY", closes, end=later)
    assert bars.loc[bars["date"] <= pd.Timestamp(AS_OF), "close"].iloc[-1] < 1_000

    basket = build_basket(bars, ["SPY"], AS_OF, created_at=CREATED)

    assert basket.tickers == ["SPY"]
    assert basket.members[0].metrics["last_bar_date"] <= AS_OF.isoformat()


@pytest.mark.unit
def test_default_universe_matches_the_upward_scan():
    assert scan.DEFAULT_UNIVERSE == ("SPY", "QQQ", "NVDA", "AAPL", "MSFT")
    assert scan.DEFAULT_UNIVERSE == upward_trend_momentum.DEFAULT_UNIVERSE


@pytest.mark.unit
def test_run_scan_fetches_a_lookback_ending_on_the_scan_date(monkeypatch):
    calls = []

    def fake_fetch(symbols, start, end, **kwargs):
        calls.append((symbols, start, end))
        return _bars("SPY", FALLING)

    monkeypatch.setattr(scan, "fetch_daily_bars", fake_fetch)

    basket = run_scan(AS_OF, ["spy", "QQQ"])

    assert calls == [(["SPY", "QQQ"], AS_OF - timedelta(days=400), AS_OF)]
    assert basket.tickers == ["SPY"]
    assert [r.ticker for r in basket.rejected] == ["QQQ"]


@pytest.mark.unit
def test_basket_round_trips_through_json(tmp_path):
    basket = build_basket(_bars("SPY", FALLING), ["SPY", "QQQ"], AS_OF, created_at=CREATED)

    path = save_basket(basket, tmp_path)

    assert path.name == "scan-breakdown-short-candidates_2026-09-30_20260930T210000Z.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["scan_name"] == "SCAN-Breakdown Short Candidates"
    assert data["as_of"] == "2026-09-30"
    assert data["members"][0]["ticker"] == "SPY"
    assert load_basket(path) == basket


@pytest.mark.unit
def test_an_existing_basket_file_is_never_overwritten(tmp_path):
    basket = build_basket(_bars("SPY", FALLING), ["SPY"], AS_OF, created_at=CREATED)
    save_basket(basket, tmp_path)
    with pytest.raises(FileExistsError):
        save_basket(basket, tmp_path)


@pytest.mark.unit
def test_the_router_still_picks_the_upward_basket(tmp_path):
    """A breakdown basket beside the upward one, even a newer one, is not routed."""
    upward = upward_trend_momentum.build_basket(_bars("SPY", RISING), ["SPY"], AS_OF, created_at=CREATED)
    up_path = save_basket(upward, tmp_path)
    breakdown = build_basket(_bars("SPY", FALLING), ["SPY"], AS_OF, created_at=CREATED + timedelta(hours=1))
    save_basket(breakdown, tmp_path)

    path, basket = latest_basket(AS_OF, tmp_path)

    assert path == up_path
    assert basket.scan_name == "SCAN-Upward Trend Momentum"


@pytest.mark.unit
def test_the_new_code_imports_no_alpaca_module_directly():
    """Bars come only through extensions.market_data.alpaca_bars; no trading client is named."""
    for module in (scan, cli):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not any(m == "alpaca" or m.startswith("alpaca.") for m in imported), module.__name__
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not {n for n in names if "order" in n.lower() or "trading" in n.lower()}, module.__name__


def _fake_run(monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(
        scan, "fetch_daily_bars",
        lambda symbols, start, end, **k: pd.concat([_bars("SPY", FALLING), _bars("AAPL", RISING)]),
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

    assert len(list((tmp_path / ".tradingagents" / "baskets").glob("scan-breakdown-*.json"))) == 1


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
