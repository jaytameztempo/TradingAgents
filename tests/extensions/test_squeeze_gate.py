"""The TTM Squeeze gate passes a long only on a bullish fire on both the 4-hour and the 1-hour regular-session bars,
a short only on a bearish fire on both, from bars dated on or before as_of. The bars are 30-minute bars read over
plain HTTPS from the market-data API and rolled into session bars. No network: a fake opener stands in."""

import json
import urllib.parse
from datetime import date

import numpy as np
import pandas as pd
import pytest

from extensions.market_data.alpaca_bars import regular_session_4hour
from extensions.status.squeeze_gate import (
    MIN_BARS,
    NEITHER,
    OFF,
    ON,
    SqueezeRead,
    _linreg_end,
    gate,
    last_fire,
    read_squeeze,
    roll_session_bars,
    squeeze_gates,
    ttm_squeeze,
)
from paper_orders.session_bars import DATA_URL, BarsReadFailed, data_reader
from paper_orders.snapshot import NotPaperKey

AS_OF = date(2026, 9, 30)
PAPER_ENV = {"ALPACA_API_KEY": "PKTESTKEY000", "ALPACA_SECRET_KEY": "secret"}


def _read(tf, fired, last_momentum=None):
    if last_momentum is None:
        last_momentum = -1.0 if fired == "bearish" else 1.0
    return SqueezeRead(tf, 100, "2026-09-30T13:30:00", OFF, last_momentum, fired, "2026-09-30T09:30:00", 1.0,
                       f"{tf} fired {fired}" if fired else f"{tf} squeeze has not fired")


# --- the squeeze ----------------------------------------------------------------

@pytest.mark.unit
def test_linreg_end_is_the_fitted_line_at_the_last_bar():
    assert _linreg_end(np.array([1.0, 3.0, 5.0, 7.0])) == pytest.approx(7.0)
    assert _linreg_end(np.array([0.0, 1.0, 0.0, 1.0])) == pytest.approx(0.8)  # slope 0.2, mean 0.5, x_end 1.5 off


@pytest.mark.unit
@pytest.mark.parametrize("states, fire", [
    ([None, ON, OFF], 2),
    ([ON, NEITHER, NEITHER, OFF, OFF], 3),  # the first outside bar after the squeeze
    ([ON, OFF, OFF, ON], None),  # back inside: a new squeeze, not fired
    ([OFF, OFF, NEITHER], None),  # never squeezed
    ([ON, OFF, ON, ON, OFF], 4),
])
def test_the_fire_is_the_first_bar_outside_after_a_bar_inside(states, fire):
    assert last_fire(pd.DataFrame({"state": states})) == fire


@pytest.mark.unit
def test_quiet_bars_are_in_a_squeeze_and_a_ramp_releases_it(squeeze_bars):
    four = roll_session_bars(squeeze_bars("UPP", AS_OF), "4h")
    sq = ttm_squeeze(four)
    assert sq["state"].iloc[:19].isna().all() and sq["momentum"].iloc[: 2 * 20 - 2].isna().all()
    assert sq["state"].iloc[40] == ON and sq["state"].iloc[-1] == OFF and sq["momentum"].iloc[-1] > 0
    assert (sq["bb_upper"] > sq["kc_upper"]).iloc[-1] and (sq["bb_lower"] < sq["kc_lower"]).iloc[-1]


@pytest.mark.unit
@pytest.mark.parametrize("side, step, direction", [("long", 0.5, "bullish"), ("short", -0.5, "bearish")])
def test_a_fire_in_the_trade_direction_on_both_timeframes_passes(squeeze_bars, side, step, direction):
    g = squeeze_gates({"X": side}, squeeze_bars("X", AS_OF, step=step), AS_OF)["X"]
    assert g.passed and g.reason is None
    assert {tf: r.fired for tf, r in g.reads.items()} == {"4h": direction, "1h": direction}
    assert g.reads["4h"].fire_bar == "2026-09-30T09:30:00" and g.reads["1h"].fire_bar == "2026-09-29T14:30:00"


@pytest.mark.unit
@pytest.mark.parametrize("side, step", [("long", -0.5), ("short", 0.5)])
def test_a_fire_against_the_trade_fails_with_both_reads_in_the_reason(squeeze_bars, side, step):
    g = squeeze_gates({"X": side}, squeeze_bars("X", AS_OF, step=step), AS_OF)["X"]
    want, got = ("bullish", "bearish") if side == "long" else ("bearish", "bullish")
    assert not g.passed
    assert g.reason == (f"TTM Squeeze not {want} on both 4-hour and 1-hour: 4-hour squeeze fired {got}, not {want}; "
                        f"1-hour squeeze fired {got}, not {want}")


@pytest.mark.unit
@pytest.mark.parametrize("four, one", [("bullish", None), (None, "bullish"), ("bullish", "bearish"), (None, None)])
def test_one_timeframe_is_not_enough(four, one):
    g = gate("X", "long", {"4h": _read("4h", four), "1h": _read("1h", one)})
    assert not g.passed and g.reason.startswith("TTM Squeeze not bullish on both 4-hour and 1-hour: ")
    assert gate("X", "long", {"4h": _read("4h", "bullish"), "1h": _read("1h", "bullish")}).passed


@pytest.mark.unit
@pytest.mark.parametrize("side, fired, four_now, one_now, passed", [
    ("long", "bullish", 1.0, 0.5, True),
    ("long", "bullish", 1.0, -0.5, False),  # a bullish fire whose latest 1-hour bar is now bearish
    ("long", "bullish", -0.5, 1.0, False),
    ("long", "bullish", 0.0, 1.0, False),  # flat is not agreeing
    ("short", "bearish", -1.0, -0.5, True),
    ("short", "bearish", -1.0, 0.5, False),
])
def test_the_latest_bars_momentum_must_still_agree_on_both(side, fired, four_now, one_now, passed):
    g = gate("X", side, {"4h": _read("4h", fired, four_now), "1h": _read("1h", fired, one_now)})
    assert g.passed is passed and (g.reason is None) is passed


@pytest.mark.unit
def test_a_bullish_fire_that_turned_bearish_on_the_latest_hour_fails(squeeze_bars):
    """Fired bullish on both, the bands are still outside the channel, but a session of selling has turned the
    latest 1-hour bar's momentum below zero; the 4-hour momentum is still above it."""
    bars = squeeze_bars("X", AS_OF, ramp_sessions=3, tail=(13, -1.0))
    g = squeeze_gates({"X": "long"}, bars, AS_OF)["X"]
    four, one = g.reads["4h"], g.reads["1h"]
    assert (four.fired, one.fired, four.last_state, one.last_state) == ("bullish", "bullish", OFF, OFF)
    assert four.last_momentum > 0 > one.last_momentum
    assert not g.passed
    assert g.reason == (f"TTM Squeeze not bullish on both 4-hour and 1-hour: 1-hour squeeze fired bullish but "
                        f"momentum on the latest bar 2026-09-30T15:30:00 is {one.last_momentum}, now bearish")


@pytest.mark.unit
def test_bars_after_as_of_are_ignored(squeeze_bars):
    """The ramp is on Oct 1 and 2; as of Sep 30 the bars are quiet and the squeeze is still on."""
    g = squeeze_gates({"X": "long"}, squeeze_bars("X", date(2026, 10, 2)), AS_OF)["X"]
    assert not g.passed
    assert all(r.fired is None and r.last_state == ON and r.last_bar.startswith("2026-09-30") for r in g.reads.values())
    assert "4-hour squeeze has not fired (last bar 2026-09-30 13:30: squeeze on)" in g.reason


@pytest.mark.unit
def test_a_squeeze_that_went_back_on_has_not_fired(squeeze_bars):
    """Ramped up to Sep 10, quiet since: the bands came back inside the channel."""
    g = squeeze_gates({"X": "long"}, squeeze_bars("X", AS_OF, ramp_end=date(2026, 9, 10)), AS_OF)["X"]
    assert not g.passed and all(r.fired is None and r.last_state == ON for r in g.reads.values())


@pytest.mark.unit
def test_a_fire_before_the_lookback_sessions_is_stale(squeeze_bars):
    one = roll_session_bars(squeeze_bars("X", AS_OF), "1h")
    assert read_squeeze(one, "1h", AS_OF, lookback_sessions=2).fired == "bullish"
    stale = read_squeeze(one, "1h", AS_OF, lookback_sessions=1)
    assert stale.fired is None and stale.fire_bar == "2026-09-29T14:30:00"
    assert stale.detail == "1-hour squeeze last fired 2026-09-29 14:30, before the last 1 sessions"


@pytest.mark.unit
def test_too_few_bars_or_none_fail_with_the_count(squeeze_bars):
    g = squeeze_gates({"X": "long", "Y": "short"}, squeeze_bars("X", AS_OF, sessions=10), AS_OF)
    assert g["X"].reads["4h"].detail == f"20 4-hour bars on or before {AS_OF}, {MIN_BARS} needed"
    assert g["X"].reads["1h"].fired == "bullish"  # 70 hourly bars are enough
    assert not g["X"].passed
    assert g["Y"].reads["4h"].bars == 0 and not g["Y"].passed


# --- the bars ---------------------------------------------------------------------

@pytest.mark.unit
def test_session_bars_drop_extended_hours_and_anchor_at_0930(squeeze_bars):
    raw = squeeze_bars("X", AS_OF, sessions=1, extended_hours=True)
    one, four = roll_session_bars(raw, "1h"), roll_session_bars(raw, "4h")
    assert [t.strftime("%H:%M") for t in one["timestamp"]] == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30",
                                                                "15:30"]
    assert one["source_bars"].tolist() == [2, 2, 2, 2, 2, 2, 1]
    assert [t.strftime("%H:%M") for t in four["timestamp"]] == ["09:30", "13:30"]
    assert four["source_bars"].tolist() == [8, 5]
    assert one["high"].max() < 900 and one["low"].min() > 1 and one["volume"].sum() == 13 * 1000


@pytest.mark.unit
def test_no_bars_read_makes_no_session_bars_and_fails_every_name():
    reader = data_reader(PAPER_ENV, opener=lambda req, timeout: _Resp(b'{"bars": {}, "next_page_token": null}'))
    raw = reader.bars_30min(["AAA"], AS_OF, AS_OF)
    assert raw.empty and roll_session_bars(raw, "1h").empty
    g = squeeze_gates({"AAA": "long"}, raw, AS_OF)["AAA"]
    assert not g.passed and "0 4-hour bars" in g.reason and "0 1-hour bars" in g.reason


@pytest.mark.unit
def test_4hour_bars_match_the_alpaca_bars_roll_up(squeeze_bars):
    raw = squeeze_bars("X", AS_OF, sessions=3, extended_hours=True)
    alpaca_shaped = raw.assign(timestamp=raw["timestamp"].dt.tz_localize("America/New_York").dt.tz_convert("UTC"))
    expected = regular_session_4hour(alpaca_shaped.set_index(["symbol", "timestamp"]))
    pd.testing.assert_frame_equal(roll_session_bars(raw, "4h"), expected, check_dtype=False)


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


@pytest.mark.unit
def test_the_reader_gets_30_minute_bars_page_by_page_from_the_data_url():
    pages = [
        {"bars": {"AAA": [{"t": "2026-09-30T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]},
         "next_page_token": "p2"},
        {"bars": {"AAA": [{"t": "2026-09-30T14:00:00Z", "o": 1.5, "h": 2, "l": 1, "c": 1.8, "v": 5}],
                  "BBB": [{"t": "2026-09-30T19:30:00Z", "o": 3, "h": 3, "l": 3, "c": 3, "v": 1}]},
         "next_page_token": None},
    ]
    seen = []

    def opener(req, timeout):
        seen.append(req)
        return _Resp(json.dumps(pages[len(seen) - 1]).encode())

    frame = data_reader(PAPER_ENV, opener=opener).bars_30min(["BBB", "AAA"], date(2026, 6, 1), AS_OF)
    assert [r.get_method() for r in seen] == ["GET", "GET"]
    assert all(r.full_url.startswith(f"{DATA_URL}/v2/stocks/bars?") for r in seen)
    first, second = (dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.full_url).query)) for r in seen)
    assert first["symbols"] == "AAA,BBB" and first["timeframe"] == "30Min" and first["feed"] == "iex"
    assert first["adjustment"] == "split" and "page_token" not in first and second["page_token"] == "p2"
    assert first["start"] == "2026-06-01T00:00:00-04:00" and first["end"] == "2026-09-30T23:59:59-04:00"
    assert frame["symbol"].tolist() == ["AAA", "AAA", "BBB"]
    assert [t.strftime("%Y-%m-%d %H:%M") for t in frame["timestamp"]] == [
        "2026-09-30 09:30", "2026-09-30 10:00", "2026-09-30 15:30"]  # New York time


@pytest.mark.unit
def test_the_reader_takes_paper_keys_only_and_reports_failures():
    with pytest.raises(NotPaperKey):
        data_reader({"ALPACA_API_KEY": "AKLIVE", "ALPACA_SECRET_KEY": "s"})

    def broken(req, timeout):
        raise OSError("connection refused")

    with pytest.raises(BarsReadFailed, match="connection refused"):
        data_reader(PAPER_ENV, opener=broken).bars_30min(["AAA"], AS_OF, AS_OF)
