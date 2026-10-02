"""RegimeBot labels UP or DOWN only when every rule confirms the trend; anything else is SIDE.

Bars here are made up; no test reaches Alpaca.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from extensions.market_data.alpaca_bars import FRAME_COLUMNS
from extensions.regime import regime_bot as bot
from extensions.regime.indicators import sma, wilder_adx
from extensions.regime.regime_bot import (
    DOWN,
    SIDE,
    UP,
    RegimeLabel,
    classify,
    decide,
    default_regime_dir,
    label_symbol,
    load_label,
    run_regime_bot,
    save_label,
    tangle_check,
)
from extensions.scripts import run_regime_bot as cli

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)


def _bars(symbol, closes, end=AS_OF, spread=0.5):
    closes = list(closes)
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    frame = pd.DataFrame({"symbol": symbol, "date": dates, "close": closes})
    frame["open"], frame["high"], frame["low"] = frame["close"], frame["close"] + spread, frame["close"] - spread
    frame["volume"], frame["trade_count"], frame["vwap"] = 1_000.0, 10.0, frame["close"]
    return frame[FRAME_COLUMNS]


RISING = [100 + i * 0.5 for i in range(260)]
FALLING = [230 - i * 0.5 for i in range(260)]
ZIGZAG = [100 + (0.5 if i % 2 else -0.5) for i in range(260)]


def _reference_adx(high, low, close, period=14):
    """Textbook ADX with Wilder's running sums, written independently of the module."""
    tr, pdm, mdm = [], [], []
    for i in range(1, len(close)):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
    tr_s, p_s, m_s = sum(tr[:period]), sum(pdm[:period]), sum(mdm[:period])
    dxs = []
    for i in range(period - 1, len(tr)):
        if i >= period:
            tr_s += tr[i] - tr_s / period
            p_s += pdm[i] - p_s / period
            m_s += mdm[i] - m_s / period
        pdi, mdi = 100 * p_s / tr_s, 100 * m_s / tr_s
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi))
    adx = [float("nan")] * (2 * period - 1)
    current = sum(dxs[:period]) / period
    adx.append(current)
    for dx in dxs[period:]:
        current = (current * (period - 1) + dx) / period
        adx.append(current)
    return adx


@pytest.mark.unit
def test_adx_matches_an_independent_textbook_calculation():
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, 120))
    high = close + rng.uniform(0.1, 1.5, 120)
    low = close - rng.uniform(0.1, 1.5, 120)

    ours = wilder_adx(pd.Series(high), pd.Series(low), pd.Series(close)).to_numpy()
    expected = np.array(_reference_adx(high, low, close))

    assert np.isnan(ours[:27]).all() and not np.isnan(ours[27:]).any()
    np.testing.assert_allclose(ours[27:], expected[27:], rtol=1e-10)


@pytest.mark.unit
def test_adx_is_100_for_a_straight_line_up_and_0_for_a_flat_line():
    rising = _bars("X", RISING[:40])
    flat = _bars("X", [100.0] * 40)
    assert wilder_adx(rising["high"], rising["low"], rising["close"]).iloc[-1] == pytest.approx(100)
    assert wilder_adx(flat["high"], flat["low"], flat["close"]).iloc[-1] == pytest.approx(0)


@pytest.mark.unit
def test_adx_needs_two_periods_of_bars():
    short = _bars("X", RISING[:27])
    assert wilder_adx(short["high"], short["low"], short["close"]).isna().all()


@pytest.mark.unit
def test_sma_waits_for_a_full_window():
    out = sma(pd.Series([1.0, 2.0, 3.0, 4.0]), 3)
    assert out.isna().tolist() == [True, True, False, False]
    assert out.iloc[-1] == 3.0


def _averages(spread_pct, above_days):
    """Slow average flat at 100; fast average below it until the last `above_days` days."""
    slow = pd.Series([100.0] * 60)
    fast = pd.Series([95.0] * (60 - above_days) + [100.0 + spread_pct] * above_days)
    return fast, slow


@pytest.mark.unit
def test_averages_within_1_percent_are_tangled():
    tangled, spread, crossed = tangle_check(*_averages(0.5, 60))
    assert tangled and not crossed
    assert spread == pytest.approx(0.5)


@pytest.mark.unit
def test_a_cross_in_the_last_20_trading_days_is_tangled():
    tangled, _, crossed = tangle_check(*_averages(5.0, 20))
    assert tangled and crossed


@pytest.mark.unit
def test_a_cross_21_trading_days_ago_is_not_tangled():
    tangled, spread, crossed = tangle_check(*_averages(5.0, 21))
    assert not tangled and not crossed
    assert spread == pytest.approx(5.0)


@pytest.mark.unit
def test_a_1_percent_spread_is_not_tangled():
    assert not tangle_check(*_averages(1.0, 60))[0]


@pytest.mark.unit
@pytest.mark.parametrize("close, sma_fast, sma_slow, adx, expected, reason", [
    (110, 105, 100, 30, UP, "aligned up"),
    (110, 105, 100, 25, UP, "at least 25"),
    (90, 95, 100, 30, DOWN, "aligned down"),
    (110, 105, 100, 22, SIDE, "between 20 and 25"),
    (110, 105, 100, 19.9, SIDE, "under 20"),
    (102, 105, 100, 30, SIDE, "neither aligned"),
    (98, 95, 100, 30, SIDE, "neither aligned"),
    (90, 95, 100, 20, SIDE, "between 20 and 25"),
])
def test_decide_applies_the_rules(close, sma_fast, sma_slow, adx, expected, reason):
    label, reasons = decide(close=close, sma_fast=sma_fast, sma_slow=sma_slow, adx=adx, tangled=False)
    assert label == expected
    assert any(reason in r for r in reasons)


@pytest.mark.unit
def test_tangled_averages_are_side_even_with_a_strong_trend():
    label, reasons = decide(close=110, sma_fast=105, sma_slow=100, adx=40, tangled=True)
    assert label == SIDE
    assert "tangled" in reasons[0]


@pytest.mark.unit
def test_every_side_trigger_is_reported():
    _, reasons = decide(close=110, sma_fast=105, sma_slow=100, adx=10, tangled=True)
    assert len(reasons) == 2


@pytest.mark.unit
@pytest.mark.parametrize("closes, expected", [(RISING, UP), (FALLING, DOWN), (ZIGZAG, SIDE)])
def test_classify_labels_whole_series(closes, expected):
    label, _, metrics = classify(_bars("SPY", closes))
    assert label == expected
    assert metrics["bars"] == 260
    assert set(metrics) >= {"close", "sma_50", "sma_200", "sma_spread_pct", "crossed_recently", "adx_14"}


@pytest.mark.unit
def test_fewer_than_200_bars_is_side():
    label, reasons, metrics = classify(_bars("SPY", RISING[:199]))
    assert label == SIDE
    assert reasons == ["only 199 daily bars; needs 200"]
    assert metrics == {"bars": 199}


@pytest.mark.unit
def test_bars_after_the_label_date_are_ignored():
    """A crash dated after the label date must not change a past label."""
    bars = _bars("SPY", RISING + [10.0] * 10, end=AS_OF + timedelta(days=14))

    label = label_symbol(bars, "SPY", AS_OF, created_at=CREATED)

    assert label.label == UP
    assert label.metrics["last_bar_date"] <= AS_OF.isoformat()


@pytest.mark.unit
def test_label_symbol_reads_only_its_own_symbol():
    bars = pd.concat([_bars("SPY", RISING), _bars("QQQ", FALLING)])
    assert label_symbol(bars, "qqq", AS_OF, created_at=CREATED).label == DOWN


@pytest.mark.unit
def test_run_regime_bot_fetches_a_lookback_ending_on_the_label_date(monkeypatch):
    calls = []

    def fake_fetch(symbols, start, end, **kwargs):
        calls.append((symbols, start, end))
        return _bars("SPY", RISING)

    monkeypatch.setattr(bot, "fetch_daily_bars", fake_fetch)

    label = run_regime_bot(AS_OF)

    assert calls == [("SPY", AS_OF - timedelta(days=400), AS_OF)]
    assert label.symbol == "SPY" and label.label == UP


@pytest.mark.unit
def test_label_round_trips_through_json_with_its_settings(tmp_path):
    label = label_symbol(_bars("SPY", RISING), "SPY", AS_OF, created_at=CREATED)

    path = save_label(label, tmp_path)

    assert path.name == "regime_SPY_2026-09-30_20260930T210000Z.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["label"] == "UP"
    assert data["parameters"]["tangle_spread_pct"] == 1.0
    assert data["parameters"]["tangle_cross_days"] == 20
    assert load_label(path) == label


@pytest.mark.unit
def test_an_existing_label_file_is_never_overwritten(tmp_path):
    label = label_symbol(_bars("SPY", RISING), "SPY", AS_OF, created_at=CREATED)
    save_label(label, tmp_path)
    with pytest.raises(FileExistsError):
        save_label(label, tmp_path)


@pytest.mark.unit
def test_an_unknown_label_is_refused_on_load():
    data = label_symbol(_bars("SPY", RISING), "SPY", AS_OF, created_at=CREATED).to_dict()
    data["label"] = "SIDEWAYS"
    with pytest.raises(ValueError, match="unknown regime label"):
        RegimeLabel.from_dict(data)


@pytest.mark.unit
def test_default_regime_dir_is_under_dot_tradingagents(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_regime_dir() == tmp_path / ".tradingagents" / "regimes"


def _fake_run(monkeypatch, closes=RISING):
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(bot, "fetch_daily_bars", lambda symbols, start, end, **k: _bars(symbols, closes))


@pytest.mark.unit
def test_cli_writes_the_label_to_out_dir(monkeypatch, tmp_path, capsys):
    _fake_run(monkeypatch, FALLING)

    code = cli.main(["--as-of", "2026-09-30", "--symbol", "qqq", "--out-dir", str(tmp_path)])

    assert code == cli.EXIT_OK
    assert "QQQ as of 2026-09-30: DOWN" in capsys.readouterr().out
    (path,) = tmp_path.glob("*.json")
    assert load_label(path).label == DOWN


@pytest.mark.unit
def test_cli_defaults_to_spy_and_the_default_folder(monkeypatch, tmp_path):
    _fake_run(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert cli.main(["--as-of", "2026-09-30"]) == cli.EXIT_OK

    (path,) = (tmp_path / ".tradingagents" / "regimes").glob("*.json")
    assert load_label(path).symbol == "SPY"


@pytest.mark.unit
@pytest.mark.parametrize("argv", [
    ["--as-of", "2026-09-30", "--symbol", "SPY;rm"],
    ["--as-of", "30/09/2026"],
    ["--as-of", (date.today() + timedelta(days=30)).isoformat()],
])
def test_cli_refuses_bad_input_before_fetching(monkeypatch, argv):
    monkeypatch.setattr(bot, "fetch_daily_bars", lambda *a, **k: pytest.fail("fetched"))
    assert cli.main(argv) == cli.EXIT_BAD_INPUT


@pytest.mark.unit
def test_cli_reports_missing_keys_without_writing_a_label(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    code = cli.main(["--as-of", "2026-09-30", "--out-dir", str(tmp_path)])

    assert code == cli.EXIT_FAILED
    assert "ALPACA_API_KEY" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
