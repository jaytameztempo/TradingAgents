"""Tradebot triggers: the fail-closed gates, the eod and intraday stages, complete-bar handling and the switches."""

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from extensions.status import confirmation
from extensions.status.confirmation import Confirmation, MarketData
from extensions.status.earnings_blackout import CLEAR, REMOVED, NameCheck
from extensions.tradebot import config, store, triggers

AS_OF = date(2026, 9, 30)  # a Wednesday
AS_OF_TEXT = AS_OF.isoformat()
BASKET = f"scan-up-category-1_{AS_OF}_20261001T000000Z.json"
EMPTY = MarketData(pd.DataFrame(), pd.DataFrame(), "test")


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _route(home: Path, label: str, as_of: date = AS_OF) -> None:
    _write(home / "routes" / f"route_ALL_{as_of}_20261001T000000Z.json",
           {"as_of": as_of.isoformat(), "created_at": f"{as_of}T22:00:00+00:00", "regime_symbol": "SPY+QQQ",
            "regime_label": label, "schema_version": 1})


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(store.HOME_ENV, str(tmp_path))
    _route(tmp_path, "UP")
    _write(tmp_path / "baskets" / BASKET, {"scan_name": "SCAN-UP Category 1", "as_of": AS_OF.isoformat(),
                                           "created_at": "2026-10-01T00:00:00+00:00",
                                           "members": [{"ticker": "X", "score": 0.1, "metrics": {}}]})
    return tmp_path


@pytest.fixture
def settings():
    return config.validate({})


@pytest.fixture
def clear_earnings(monkeypatch):
    def fake(symbol, side, as_of, _dir):
        return NameCheck(symbol, side, "2026-11-20", "estimated", "est", 36, CLEAR, None,
                         "2026-09-29T00:00:00+00:00"), []
    monkeypatch.setattr(triggers, "check_name", fake)


def _daily(monkeypatch, passed=True, triggers_=("rsi",), reasons=()):
    def fake(symbol, side, market, as_of):
        return Confirmation(symbol, side, passed, list(reasons), list(triggers_), {}, 40.0, 900.0, 1000.0,
                            as_of.isoformat())
    monkeypatch.setattr(confirmation, "confirm", fake)


def _item(status="waiting", bot="UP", side="long", as_of=AS_OF_TEXT, expires="2026-10-14"):
    return {"id": "i1", "ticker": "X", "kind": "up_cat", "scan_file": BASKET, "side": side, "bot": bot,
            "status": status, "as_of": as_of, "expires_after": expires}


def _check(item, settings, stage="eod", bars=None, now=None, home=None):
    label, _ = triggers.route_label(AS_OF, stage)
    return triggers.check_item(item, stage, AS_OF, label, settings, EMPTY,
                               bars if bars is not None else pd.DataFrame(), Path(home or ".") / "fundamentals", now)


# --- gates ---------------------------------------------------------------------

@pytest.mark.unit
def test_a_label_that_is_not_the_bot_blocks(home, settings, clear_earnings):
    _route(home, "SIDE")
    c = _check(_item(), settings)
    assert c.status == "blocked" and "market label is SIDE" in c.reason


@pytest.mark.unit
def test_no_route_blocks(home, settings, clear_earnings):
    (home / "routes" / f"route_ALL_{AS_OF}_20261001T000000Z.json").unlink()
    assert _check(_item(), settings).status == "blocked"


@pytest.mark.unit
def test_past_its_last_session_it_expires(home, settings, clear_earnings):
    c = _check(_item(expires="2026-09-29"), settings)
    assert c.status == "expired"


@pytest.mark.unit
def test_dropping_out_of_the_newest_basket_expires_it(home, settings, clear_earnings):
    _write(home / "baskets" / f"scan-up-category-1_{AS_OF}_20261001T090000Z.json",
           {"scan_name": "SCAN-UP Category 1", "as_of": AS_OF.isoformat(), "created_at": "2026-10-01T09:00:00+00:00",
            "members": [{"ticker": "Y", "score": 0.1, "metrics": {}}]})
    c = _check(_item(), settings)
    assert c.status == "expired" and "not in the newest scan" in c.reason


@pytest.mark.unit
def test_no_earnings_cache_blocks_by_default(home, settings, monkeypatch):
    _daily(monkeypatch)
    c = _check(_item(), settings, home=home)  # the real check_name, an empty fundamentals folder
    assert c.status == "blocked" and "earnings date unknown" in c.reason
    assert c.gates["earnings"]["source"].startswith("SEC EDGAR")


@pytest.mark.unit
def test_earnings_unknown_can_be_allowed(home, monkeypatch):
    _daily(monkeypatch)
    c = _check(_item(), config.validate({"earnings_unknown": "allow", "filters": {"squeeze_4h1h": False}}), home=home)
    assert c.status == "triggered"


@pytest.mark.unit
def test_a_stale_earnings_cache_blocks(home, settings, monkeypatch):
    def old(symbol, side, as_of, _dir):
        return NameCheck(symbol, side, "2026-11-20", "estimated", "est", 36, CLEAR, None, "2026-07-01T00:00:00+00:00"), []
    monkeypatch.setattr(triggers, "check_name", old)
    c = _check(_item(), settings)
    assert c.status == "blocked" and "days old" in c.reason


@pytest.mark.unit
def test_earnings_inside_the_blackout_blocks(home, settings, monkeypatch):
    def near(symbol, side, as_of, _dir):
        return NameCheck(symbol, side, "2026-10-02", "filed", "f", 2, REMOVED, None, "2026-09-29T00:00:00+00:00"), []
    monkeypatch.setattr(triggers, "check_name", near)
    assert _check(_item(), settings).status == "blocked"


# --- eod stage -------------------------------------------------------------------

@pytest.mark.unit
def test_eod_daily_pass_arms_a_waiting_item(home, settings, clear_earnings, monkeypatch):
    _daily(monkeypatch)
    c = _check(_item(), settings)
    assert c.status == "armed" and c.daily["triggers"] == ["rsi"] and c.squeeze["passed"] is False


@pytest.mark.unit
def test_eod_daily_pass_with_the_squeeze_fired_triggers(home, settings, clear_earnings, monkeypatch, squeeze_bars):
    _daily(monkeypatch)
    c = _check(_item(), settings, bars=squeeze_bars("X", AS_OF, step=0.5))
    assert c.status == "triggered" and c.passed


@pytest.mark.unit
def test_eod_daily_fail_returns_an_armed_item_to_waiting(home, settings, clear_earnings, monkeypatch):
    _daily(monkeypatch, passed=False, triggers_=(), reasons=(f"{confirmation.AWAITING_TRIGGER} (x)",))
    assert _check(_item("armed"), settings).status == "waiting"
    assert _check(_item("waiting"), settings).status is None


@pytest.mark.unit
def test_missing_daily_bars_never_pass_even_with_every_filter_off(home, clear_earnings, monkeypatch):
    _daily(monkeypatch, passed=False, triggers_=(), reasons=(f"{confirmation.NO_DAILY_BAR} (x)",))
    off = config.validate({"filters": dict.fromkeys(config.FILTERS, False)})
    c = _check(_item(), off)
    assert c.status is None and confirmation.NO_DAILY_BAR in c.reason


@pytest.mark.unit
def test_switching_off_light_volume_lets_heavy_volume_through(home, clear_earnings, monkeypatch):
    _daily(monkeypatch, passed=False, triggers_=("rsi",), reasons=(f"{confirmation.VOLUME_NOT_LIGHT} (x)",))
    on = config.validate({})
    off = config.validate({"filters": {"light_volume": False}})
    assert _check(_item(), on).status is None
    c = _check(_item(), off)
    assert c.status == "armed" and c.filters["light_volume"] is False


@pytest.mark.unit
def test_switching_off_the_squeeze_gate_triggers_at_eod(home, clear_earnings, monkeypatch):
    _daily(monkeypatch)
    c = _check(_item(), config.validate({"filters": {"squeeze_4h1h": False}}))
    assert c.status == "triggered" and c.squeeze is None


# --- intraday stage ----------------------------------------------------------------

@pytest.mark.unit
def test_intraday_checks_armed_items_only(home, settings, clear_earnings, squeeze_bars):
    c = _check(_item("waiting"), settings, stage="intraday", bars=squeeze_bars("X", AS_OF, step=0.5),
               now=datetime(2026, 9, 30, 15, 0))
    assert c.status is None and "not armed" in c.reason


@pytest.mark.unit
def test_intraday_fire_on_complete_bars_triggers(home, settings, clear_earnings, squeeze_bars):
    bars = squeeze_bars("X", AS_OF, step=0.5, ramp_end=date(2026, 9, 29))
    c = _check(_item("armed"), settings, stage="intraday", bars=bars, now=datetime(2026, 9, 30, 10, 35))
    assert c.status == "triggered", c.reason


@pytest.mark.unit
def test_intraday_uses_the_last_close_route(home, settings, clear_earnings, squeeze_bars):
    (home / "routes" / f"route_ALL_{AS_OF}_20261001T000000Z.json").unlink()
    _route(home, "UP", date(2026, 9, 29))
    label, name = triggers.route_label(AS_OF, "intraday")
    assert label == "UP" and "2026-09-29" in name
    assert triggers.route_label(AS_OF, "eod") == (None, None)


# --- complete bars -------------------------------------------------------------------

@pytest.mark.unit
def test_bars_still_forming_are_dropped(squeeze_bars):
    bars = squeeze_bars("X", AS_OF)
    now = datetime(2026, 9, 30, 11, 5)
    one = triggers.complete_session_bars(bars, "1h", now)
    four = triggers.complete_session_bars(bars, "4h", now)
    assert one["timestamp"].iloc[-1] == pd.Timestamp("2026-09-30 09:30")  # 10:30-11:30 is still forming
    assert four["timestamp"].iloc[-1] == pd.Timestamp("2026-09-29 13:30")  # today's 09:30-13:30 is still forming
    after = triggers.complete_session_bars(bars, "4h", datetime(2026, 9, 30, 13, 35))
    assert after["timestamp"].iloc[-1] == pd.Timestamp("2026-09-30 09:30")


@pytest.mark.unit
def test_a_bar_missing_parts_is_dropped(squeeze_bars):
    bars = squeeze_bars("X", AS_OF)
    gap = bars[bars["timestamp"] != pd.Timestamp("2026-09-29 10:00")]  # one 30-minute bar of the 09:30 hour lost
    one = triggers.complete_session_bars(gap, "1h", None)
    assert pd.Timestamp("2026-09-29 09:30") not in set(one["timestamp"])
    assert pd.Timestamp("2026-09-29 10:30") in set(one["timestamp"])


@pytest.mark.unit
def test_extended_hours_bars_are_ignored(squeeze_bars):
    plain = triggers.complete_session_bars(squeeze_bars("X", AS_OF), "1h", None)
    extended = triggers.complete_session_bars(squeeze_bars("X", AS_OF, extended_hours=True), "1h", None)
    pd.testing.assert_frame_equal(plain, extended)


@pytest.mark.unit
def test_the_last_hour_of_the_session_is_one_thirty_minute_bar(squeeze_bars):
    one = triggers.complete_session_bars(squeeze_bars("X", AS_OF), "1h", None)
    last = one[one["timestamp"] == pd.Timestamp("2026-09-30 15:30")]
    assert len(last) == 1 and int(last["source_bars"].iloc[0]) == 1
