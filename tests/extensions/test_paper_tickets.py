"""Paper tickets read the newest daily status, the earnings blackout built from it, the ranked side handoff it
names and the channel reports that handoff ranked. Only the blackout's allowed names are ticketed: entry at the
channel line, pivot the latest confirmed daily ZigZag swing beyond the entry (a low below a long entry, a
high above a short one; none, no ticket), stop the pivot -/+ 1 ATR snapped to .99/.49 (long) or .01/.51 (short),
target at the midline. Buying power comes from the paper
account at ticket time (a fake here). Each of the N allowed names with a usable pivot gets buying power / N, at most
15% of buying power, and floor(that / entry) shares whatever its stop; no name is shrunk or skipped because
another used the budget. Blackout names are listed as skipped with the reason. No label but SIDE writes anything. Alpaca's trading client is refused. No network, no orders.
"""

import json
import subprocess
import sys
import types
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from extensions.scripts import run_paper_tickets as cli
from extensions.status.paper_tickets import (
    TICKET_VERSION,
    Account,
    TicketInputError,
    TradingClientLoaded,
    account_from_alpaca,
    build_ticket,
    build_tickets,
    name_notional_limit,
    refuse_trading_client,
    save_tickets,
    snap_stop,
    stop_for_fill,
    stop_pivot,
    tickets_for_date,
)

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
STATUS_NAME = "daily_status_2026-09-30_20261008T231343Z.json"
BLACKOUT_NAME = "earnings_blackout_2026-09-30_20261008T232454Z.json"
HANDOFF_NAME = "ranked_side_handoff_2026-09-30_20261008T110409Z.json"
LONG_CHANNEL_NAME = "long_channel_2026-09-30_20261008T100000Z.json"
SHORT_CHANNEL_NAME = "short_channel_2026-09-30_20261008T100001Z.json"
VST = {"support": 138.4525, "resistance": 156.99, "atr_14": 4.6929, "midline": 147.7212}
WIDE = {"support": 10.0, "resistance": 20.0, "atr_14": 8.0, "midline": 15.0}
SHORTY = {"support": 40.0, "resistance": 50.0, "atr_14": 2.0, "midline": 45.0}
TEN = {"support": 10.0, "resistance": 20.0, "atr_14": 1.0, "midline": 15.0}  # stop = swing low - 1.00, snapped
ACCOUNT = Account(equity=5000.0, buying_power=20000.0, read_at=CREATED, source="fake paper account", status="ACTIVE")


def _swing(price, day, confirmed=None):
    return {"date": f"2026-09-{day:02d}", "price": price, "confirmed_on": f"2026-09-{confirmed or day + 2:02d}"}


# the last confirmed swing is the second one in each list
VST_LOWS = [_swing(137.0, 10), _swing(136.0, 20), _swing(140.0, 5, confirmed=8)]  # 136.00 - 4.6929 = 131.3071 -> 130.99
WIDE_LOWS = [_swing(9.0, 3), _swing(9.5, 15)]  # WID 9.50 - 8.00 = 1.50 -> 1.49
SHORTY_HIGHS = [_swing(51.0, 4), _swing(50.4, 22)]  # SHO 50.40 + 2.00 = 52.40 -> 52.51
SWINGS = {"VST": VST_LOWS, "WID": WIDE_LOWS, "OUT": WIDE_LOWS, "SHO": SHORTY_HIGHS}


def _ticket(symbol, side, levels, swings=None, earnings_result=None, notional_limit=3000.0):
    return build_ticket(symbol, side, levels, swings if swings is not None else SWINGS[symbol], earnings_result,
                        notional_limit=notional_limit, as_of=AS_OF)


def _never_read():
    raise AssertionError("the account was read")


def _status(label="SIDE", long=("VST", "WID", "OUT"), short=("SHO",), blocked=False):
    return {"as_of": AS_OF.isoformat(), "created_at": "2026-10-08T23:13:43+00:00", "market_label": label,
            "channel_names_blocked": blocked, "confirmed": {"long": list(long), "short": list(short)},
            "sources": {"ranked_side_handoff": f"C:\\elsewhere\\{HANDOFF_NAME}"},
            "orders_placed": False, "status_version": 1}


def _blackout(allowed_long=("VST", "WID"), allowed_short=("SHO",), status_file=STATUS_NAME, label="SIDE"):
    names = [{"symbol": "VST", "result": "clear"}, {"symbol": "WID", "result": "unknown_kept"},
             {"symbol": "OUT", "result": "removed", "earnings_date": "2026-10-01",
              "date_source": "filed", "sessions_away": 1}, {"symbol": "SHO", "result": "flagged_kept"}]
    return {"as_of": AS_OF.isoformat(), "created_at": "2026-10-08T23:24:54+00:00",
            "daily_status_file": f"C:\\elsewhere\\{status_file}", "market_label": label,
            "allowed": {"long": list(allowed_long), "short": list(allowed_short)}, "removed": ["OUT"],
            "names": names, "warnings": ["estimated dates"], "orders_placed": False, "report_version": 1}


def _name(side, levels, rank):
    return {"side": side, "setup": "LONG_FADE" if side == "long" else "SHORT_FADE", "status": "PLAN",
            "rank": rank, "levels": levels}


def _handoff(names=None):
    return {"as_of": AS_OF.isoformat(), "created_at": "2026-10-08T11:04:09+00:00",
            "long_channel_report": f"C:\\elsewhere\\{LONG_CHANNEL_NAME}",
            "short_channel_report": f"C:\\elsewhere\\{SHORT_CHANNEL_NAME}",
            "names": names or {"VST": _name("long", VST, 1), "WID": _name("long", WIDE, 2),
                               "OUT": _name("long", WIDE, 3), "SHO": _name("short", SHORTY, 1)}}


def _report(side, swings_by_symbol):
    key = "support_touches" if side == "long" else "resistance_touches"
    return {"as_of": AS_OF.isoformat(), "calls": {s: {"metrics": {key: sw}} for s, sw in swings_by_symbol.items()}}


def _channels(swings=None):
    swings = swings or SWINGS
    return {"long": (Path(LONG_CHANNEL_NAME), _report("long", {s: swings[s] for s in swings if s != "SHO"})),
            "short": (Path(SHORT_CHANNEL_NAME), _report("short", {s: swings[s] for s in swings if s == "SHO"}))}


def _build_longs(spec):
    """A SIDE day whose allowed names are these longs on TEN levels, ranked in the order given: symbol -> swing low."""
    symbols = list(spec)
    names = {s: _name("long", TEN, i) for i, s in enumerate(symbols, start=1)}
    channels = {"long": (Path(LONG_CHANNEL_NAME), _report("long", {s: [_swing(p, 20)] for s, p in spec.items()}))}
    return _build(status=_status(long=symbols, short=()), blackout=_blackout(allowed_long=symbols, allowed_short=()),
                  handoff=_handoff(names), channels=channels)


def _build(status=None, blackout=None, handoff=None, account=ACCOUNT, channels=None):
    return build_tickets(AS_OF, (Path(STATUS_NAME), status or _status()),
                         (Path(BLACKOUT_NAME), blackout or _blackout()),
                         (Path(HANDOFF_NAME), handoff or _handoff()), account, created_at=CREATED,
                         channels=channels if channels is not None else _channels())


def _write(folder, name, data):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(data), encoding="utf-8")


def _write_day(tmp_path, status=None):
    _write(tmp_path / "status", STATUS_NAME, status or _status())
    _write(tmp_path / "status", BLACKOUT_NAME, _blackout())
    _write(tmp_path / "handoffs", HANDOFF_NAME, _handoff())
    channels = _channels()
    _write(tmp_path / "scanbot", LONG_CHANNEL_NAME, channels["long"][1])
    _write(tmp_path / "scanbot", SHORT_CHANNEL_NAME, channels["short"][1])
    return ["--as-of", "2026-09-30", "--status-dir", str(tmp_path / "status"), "--handoff-dir",
            str(tmp_path / "handoffs"), "--scan-dir", str(tmp_path / "scanbot")]


@pytest.fixture
def restore_meta_path(monkeypatch):
    """cli.main installs the import refusal; keep it out of the rest of the test session.

    Other test modules may have loaded alpaca.trading in this session; hide it for the test so the CLI runs.
    """
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    for name in [m for m in sys.modules if m == "alpaca.trading" or m.startswith("alpaca.trading.")]:
        monkeypatch.delitem(sys.modules, name)


@pytest.mark.unit
def test_long_stop_is_one_atr_below_the_pivot_snapped_down_to_99_or_49():
    t = _ticket("VST", "long", VST, earnings_result="clear")
    assert (t.order_side, t.entry, t.target) == ("buy", 138.45, 147.72)
    assert t.pivot == {"date": "2026-09-20", "price": 136.0, "confirmed_on": "2026-09-22"}
    assert (t.atr_14, t.stop_calculated, t.stop) == (4.6929, 131.3071, 130.99)
    assert t.stop_offset == t.risk_per_share == 7.46 and t.reward_per_share == 9.27 and t.reward_risk == 1.24
    assert t.quantity == 21 and t.notional_limit_dollars == 3000.0  # floor($3,000 / $138.45)
    assert t.notional == 2907.45 and t.risk_dollars == 156.66
    assert t.paper is True


@pytest.mark.unit
def test_short_stop_is_one_atr_above_the_pivot_snapped_up_to_01_or_51():
    t = _ticket("SHO", "short", SHORTY)
    assert (t.order_side, t.line, t.entry, t.target) == ("sell", "resistance", 50.0, 45.0)
    assert t.pivot["price"] == 50.4 and (t.stop_calculated, t.stop, t.stop_offset) == (52.4, 52.51, 2.51)
    assert t.quantity == 60 and t.notional == 3000.0 and t.risk_dollars == 150.6 and t.reward_risk == 1.99


@pytest.mark.unit
@pytest.mark.parametrize("level, stop", [
    (47.30, 46.99), (47.60, 47.49), (47.49, 47.49), (47.99, 47.99), (48.00, 47.99), (47.50, 47.49),
    (47.4899, 46.99), (131.3071, 130.99), (1.5, 1.49),
])
def test_a_long_stop_snaps_down_to_the_nearer_99_or_49_at_or_below(level, stop):
    assert snap_stop("long", level) == stop


@pytest.mark.unit
@pytest.mark.parametrize("level, stop", [
    (50.60, 51.01), (50.51, 50.51), (50.52, 51.01), (50.01, 50.01), (50.00, 50.01), (49.995, 50.01),
    (50.5001, 50.51), (52.4, 52.51),
])
def test_a_short_stop_snaps_up_to_the_nearer_01_or_51_at_or_above(level, stop):
    assert snap_stop("short", level) == stop


@pytest.mark.unit
def test_the_stop_pivot_is_the_latest_confirmed_swing_beyond_the_entry_and_never_after_as_of():
    swings = [_swing(5.0, 10, confirmed=20), _swing(6.0, 18, confirmed=19), _swing(7.0, 28, confirmed=30)]
    assert stop_pivot(swings, "long", 8.0, AS_OF)["price"] == 7.0
    assert stop_pivot(swings, "long", 8.0, date(2026, 9, 29))["price"] == 5.0  # confirmed last, though its low came first
    with pytest.raises(ValueError, match="no confirmed swing low in the long channel report"):
        stop_pivot(swings, "long", 8.0, date(2026, 9, 18))


@pytest.mark.unit
def test_a_long_pivot_passes_over_swing_lows_at_or_above_the_entry():
    swings = [_swing(5.0, 10), _swing(6.5, 15), _swing(7.0, 20), _swing(7.5, 25)]
    assert stop_pivot(swings, "long", 7.0, AS_OF)["price"] == 6.5  # 7.5 is above the entry, 7.0 sits on it


@pytest.mark.unit
def test_a_short_pivot_passes_over_swing_highs_at_or_below_the_entry():
    swings = [_swing(52.0, 10), _swing(51.0, 15), _swing(50.0, 20), _swing(49.0, 25)]
    assert stop_pivot(swings, "short", 50.0, AS_OF)["price"] == 51.0


@pytest.mark.unit
def test_a_long_whose_last_swing_low_is_above_support_stops_under_the_one_before():
    """The latest swing low 139.00 is above the 138.45 entry; the pivot is 137.00: 132.3071, snapped to 131.99."""
    t = _ticket("VST", "long", VST, swings=[_swing(137.0, 10), _swing(139.0, 25)])
    assert t.pivot["price"] == 137.0 and (t.stop_calculated, t.stop, t.stop_offset) == (132.3071, 131.99, 6.46)


@pytest.mark.unit
def test_a_short_whose_last_swing_high_is_below_resistance_stops_over_the_one_before():
    t = _ticket("SHO", "short", SHORTY, swings=[_swing(51.0, 10), _swing(49.0, 25)])
    assert t.pivot["price"] == 51.0 and (t.stop_calculated, t.stop, t.stop_offset) == (53.0, 53.01, 3.01)


@pytest.mark.unit
def test_size_does_not_depend_on_the_stop_distance():
    """$3,000 at a $138.45 entry is 21 shares, whether the stop is $7.46 or $6.46 away."""
    wide, tight = _ticket("VST", "long", VST), _ticket("VST", "long", VST, swings=[_swing(137.0, 10)])
    assert (wide.stop_offset, wide.quantity) == (7.46, 21) and (tight.stop_offset, tight.quantity) == (6.46, 21)
    t = _ticket("WID", "long", WIDE)
    assert (t.entry, t.stop_calculated, t.stop, t.stop_offset, t.quantity, t.risk_dollars) == (
        10.0, 1.5, 1.49, 8.51, 300, 2553.0)


@pytest.mark.unit
@pytest.mark.parametrize("symbol, side, levels", [("VST", "long", VST), ("WID", "long", WIDE),
                                                  ("SHO", "short", SHORTY)])
def test_the_ticket_stop_is_stop_for_fill_at_the_entry(symbol, side, levels):
    """The sender checks this, then places stop_for_fill at the actual fill."""
    t = _ticket(symbol, side, levels)
    assert t.stop == stop_for_fill(side, t.stop_offset, t.entry)
    assert (t.stop < t.entry) if side == "long" else (t.stop > t.entry)


@pytest.mark.unit
@pytest.mark.parametrize("fill, stop", [
    (138.45, 130.99),  # at the entry: the ticket's stop
    (138.10, 130.64),  # a little better
    (128.00, 120.54),  # gapped below the planned stop of 130.99
])
def test_a_long_stop_follows_the_fill_and_stays_below_it(fill, stop):
    t = _ticket("VST", "long", VST)
    assert stop_for_fill("long", t.stop_offset, fill) == stop
    assert stop < fill and round(fill - stop, 2) == t.risk_per_share


@pytest.mark.unit
@pytest.mark.parametrize("fill, stop", [
    (50.00, 52.51),  # at the entry: the ticket's stop
    (50.20, 52.71),  # a little better
    (53.25, 55.76),  # gapped above the planned stop of 52.51
])
def test_a_short_stop_follows_the_fill_and_stays_above_it(fill, stop):
    t = _ticket("SHO", "short", SHORTY)
    assert stop_for_fill("short", t.stop_offset, fill) == stop
    assert stop > fill and round(stop - fill, 2) == t.risk_per_share


@pytest.mark.unit
@pytest.mark.parametrize("side, offset, fill, match", [
    ("long", 0, 10.0, "above zero"),
    ("long", 0.5, 0, "above zero"),
    ("long", 0.5, 0.5, "not beyond fill"),  # the stop would be at zero
    ("flat", 0.5, 10.0, "not long or short"),
])
def test_stop_for_fill_refuses_what_cannot_be_a_stop(side, offset, fill, match):
    with pytest.raises(ValueError, match=match):
        stop_for_fill(side, offset, fill)


@pytest.mark.unit
@pytest.mark.parametrize("levels, swings, match", [
    ({**VST, "atr_14": 0}, VST_LOWS, "no usable"),
    ({k: v for k, v in VST.items() if k != "midline"}, VST_LOWS, "no usable"),
    (VST, [], "no swing lows defining support"),
    (VST, None, "no swing lows defining support"),
    (VST, [_swing(139.0, 25)], "no confirmed swing low below entry 138.45"),  # every swing low is above the entry
    (VST, [_swing(138.45, 25)], "no confirmed swing low below entry 138.45"),  # at the entry is not below it
    ({**VST, "midline": 130.0}, VST_LOWS, "profit side"),
    ({"support": 4.0, "resistance": 6.0, "atr_14": 3.0, "midline": 5.0}, [_swing(2.5, 20)], "not above zero"),
    ({**VST, "support": 20_000.0, "midline": 20_100.0}, [_swing(19_000.0, 20)], "no whole share"),
])
def test_unusable_levels_or_swings_make_no_ticket(levels, swings, match):
    with pytest.raises(ValueError, match=match):
        build_ticket("VST", "long", levels, swings, notional_limit=3000.0, as_of=AS_OF)


@pytest.mark.unit
def test_a_short_with_no_swing_high_above_the_entry_makes_no_ticket():
    with pytest.raises(ValueError, match=r"no confirmed swing high above entry 50.0 .*the latest 49.0 of 2026-09-25"):
        build_ticket("SHO", "short", SHORTY, [_swing(48.0, 10), _swing(49.0, 25)], notional_limit=3000.0, as_of=AS_OF)


@pytest.mark.unit
def test_name_notional_is_buying_power_over_n_capped_at_15_pct():
    assert name_notional_limit(20000.0, 3) == 3000.0  # $6,666.67 is over the 15% cap
    assert name_notional_limit(20000.0, 7) == 2857.14  # under the cap, rounded down
    assert name_notional_limit(20000.0, 1) == 3000.0 and name_notional_limit(20000.0, 10) == 2000.0
    with pytest.raises(ValueError):
        name_notional_limit(20000.0, 0)


@pytest.mark.unit
def test_account_comes_from_the_alpaca_account_body():
    a = account_from_alpaca({"equity": "101234.56", "buying_power": "404938.24", "status": "ACTIVE"}, CREATED, "x")
    assert (a.equity, a.buying_power, a.status, a.read_at) == (101234.56, 404938.24, "ACTIVE", CREATED)
    with pytest.raises(TicketInputError, match="no usable equity"):
        account_from_alpaca({"buying_power": "1"}, CREATED, "x")
    with pytest.raises(TicketInputError, match="buying_power is 0.0"):
        account_from_alpaca({"equity": "1", "buying_power": "0"}, CREATED, "x")


@pytest.mark.unit
def test_every_name_gets_buying_power_over_n_capped_at_15_pct():
    """N = 3 (OUT is in the blackout): $20,000 / 3 is over the 15% cap, so $3,000 a name. VST at $138.45 gets 21
    shares, SHO at $50 60, WID at $10 300, whatever their stops; none is shrunk or skipped for a risk budget."""
    result = _build()
    by = {t.symbol: t for t in result.tickets}
    assert all(t.notional_limit_dollars == 3000.0 for t in result.tickets)
    assert (by["VST"].quantity, by["VST"].notional, by["VST"].risk_dollars) == (21, 2907.45, 156.66)
    assert (by["SHO"].quantity, by["SHO"].notional, by["SHO"].risk_dollars) == (60, 3000.0, 150.6)
    assert (by["WID"].quantity, by["WID"].notional, by["WID"].risk_dollars) == (300, 3000.0, 2553.0)
    assert result.total_notional == 8907.45 and result.total_risk == 2860.26
    assert result.skipped == {"OUT": "in the earnings blackout: earnings 2026-10-01 (filed), 1 sessions away"}
    assert result.rules["names_n"] == 3 and result.rules["name_notional_limit"] == 3000.0
    assert result.rules["account_equity"] == 5000.0 and result.rules["buying_power"] == 20000.0
    assert result.rules["stop_atr_beyond_pivot"] == 1.0 and result.rules["name_notional_pct_of_buying_power"] == 0.15


@pytest.mark.unit
def test_sizing_follows_the_buying_power_read():
    rich = Account(equity=100000.0, buying_power=200000.0, read_at=CREATED, source="fake")
    result = _build(account=rich)
    by = {t.symbol: t for t in result.tickets}
    assert result.rules["name_notional_limit"] == 30000.0 and by["VST"].notional_limit_dollars == 30000.0
    assert (by["VST"].quantity, by["SHO"].quantity, by["WID"].quantity) == (216, 600, 3000)
    assert result.total_notional == 89905.2


@pytest.mark.unit
def test_a_side_day_without_the_account_stops():
    with pytest.raises(TicketInputError, match="paper account"):
        _build(account=None)


@pytest.mark.unit
def test_a_side_day_without_the_channel_report_stops():
    with pytest.raises(TicketInputError, match="short channel report"):
        _build(channels={"long": _channels()["long"]})


@pytest.mark.unit
def test_a_blackout_that_allows_no_names_makes_no_tickets_and_needs_no_account():
    result = _build(blackout=_blackout(allowed_long=(), allowed_short=()), account=None, channels={})
    assert result.tickets == [] and "allows no names" in result.no_ticket_reason


@pytest.mark.unit
def test_only_allowed_names_are_ticketed_and_flags_carry_over():
    result = _build()
    assert [(t.symbol, t.side) for t in result.tickets] == [("VST", "long"), ("SHO", "short"), ("WID", "long")]
    assert "OUT" not in {t.symbol for t in result.tickets}  # removed by the blackout
    assert "OUT: no ticket, in the earnings blackout: earnings 2026-10-01 (filed), 1 sessions away" in result.warnings
    assert result.total_notional == 8907.45
    assert {t.symbol: t.earnings_result for t in result.tickets} == {
        "VST": "clear", "WID": "unknown_kept", "SHO": "flagged_kept"}
    assert "WID: earnings result unknown_kept" in result.warnings and "estimated dates" in result.warnings
    assert result.orders_placed is False and result.paper is True and result.no_ticket_reason is None
    assert result.sources["long_channel_report"] == LONG_CHANNEL_NAME
    assert result.sources["short_channel_report"] == SHORT_CHANNEL_NAME


@pytest.mark.unit
def test_a_name_without_swings_in_the_channel_report_is_skipped_not_ticketed():
    result = _build(channels=_channels({k: v for k, v in SWINGS.items() if k != "WID"}))
    assert [t.symbol for t in result.tickets] == ["VST", "SHO"]
    assert "no swing lows defining support" in result.skipped["WID"]
    assert result.rules["names_n"] == 2 and all(t.notional_limit_dollars == 3000.0 for t in result.tickets)


@pytest.mark.unit
def test_tickets_are_listed_in_rank_order_not_list_order():
    result = _build(blackout=_blackout(allowed_long=("WID", "VST")))
    assert [(t.symbol, t.rank) for t in result.tickets] == [("VST", 1), ("SHO", 1), ("WID", 2)]


@pytest.mark.unit
def test_every_name_gets_the_same_capital_however_the_stops_differ():
    """N = 7, $20,000 / 7 = $2,857.14 a name (under the 15% cap): 285 shares at $10 each, though E's stop is 7.49 and
    the rest 8.49."""
    result = _build_longs({"A": 9.75, "B": 9.75, "C": 9.75, "D": 9.75, "E": 8.6, "F": 9.95, "G": 9.75})
    assert [(t.symbol, t.stop, t.quantity) for t in result.tickets] == [
        ("A", 8.49, 285), ("B", 8.49, 285), ("C", 8.49, 285), ("D", 8.49, 285), ("E", 7.49, 285), ("F", 8.49, 285),
        ("G", 8.49, 285)]
    assert result.rules["name_notional_limit"] == 2857.14 and result.total_notional == 19950.0
    assert list(result.skipped) == ["OUT"]  # only the blackout name


@pytest.mark.unit
def test_a_name_with_no_swing_below_the_entry_is_skipped_and_not_counted_in_n():
    result = _build_longs({"A": 9.75, "B": 10.5})
    assert [(t.symbol, t.quantity, t.notional_limit_dollars) for t in result.tickets] == [("A", 300, 3000.0)]
    assert result.rules["names_n"] == 1
    assert result.skipped["B"].startswith("no confirmed swing low below entry 10.0")


@pytest.mark.unit
@pytest.mark.parametrize("label", ["UP", "DOWN"])
def test_no_tickets_unless_side(label):
    result = build_tickets(AS_OF, (Path(STATUS_NAME), _status(label=label)), created_at=CREATED)
    assert result.tickets == [] and "not SIDE" in result.no_ticket_reason
    with pytest.raises(ValueError, match="no tickets to write"):
        save_tickets(result)


@pytest.mark.unit
def test_no_tickets_when_the_status_blocks_channel_names():
    result = build_tickets(AS_OF, (Path(STATUS_NAME), _status(blocked=True)), created_at=CREATED)
    assert result.tickets == [] and "blocks the channel names" in result.no_ticket_reason


@pytest.mark.unit
def test_blackout_from_another_status_stops():
    with pytest.raises(TicketInputError, match="not the newest daily status"):
        _build(blackout=_blackout(status_file="daily_status_2026-09-30_20261001T000000Z.json"))
    with pytest.raises(TicketInputError, match="records label"):
        _build(blackout=_blackout(label="UP"))


@pytest.mark.unit
def test_allowed_name_not_confirmed_stops():
    with pytest.raises(TicketInputError, match="not confirmed"):
        _build(blackout=_blackout(allowed_long=("VST", "NEW")))


@pytest.mark.unit
def test_save_writes_json_and_never_overwrites(tmp_path):
    result = _build()
    path = save_tickets(result, tmp_path)
    assert path.name == "paper_tickets_2026-09-30_20261008T120000Z.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["orders_placed"] is False and data["paper"] is True and data["as_of"] == "2026-09-30"
    assert data["ticket_version"] == TICKET_VERSION == 4
    first = data["tickets"][0]
    assert first["pivot"] == {"date": "2026-09-20", "price": 136.0, "confirmed_on": "2026-09-22"}
    assert (first["atr_14"], first["stop_calculated"], first["stop"], first["stop_offset"]) == (
        4.6929, 131.3071, 130.99, 7.46)
    assert first["notional_limit_dollars"] == 3000.0 and data["rules"]["account_source"] == "fake paper account"
    assert "risk_limit_dollars" not in first and "stop_swing" not in first and "book_risk_limit" not in data["rules"]
    assert data["total_notional"] == 8907.45 and "OUT" in data["skipped"]
    with pytest.raises(FileExistsError):
        save_tickets(result, tmp_path)


@pytest.mark.unit
def test_cli_prints_and_writes(tmp_path, capsys, restore_meta_path):
    out_dir = tmp_path / "out"
    code = cli.main(_write_day(tmp_path) + ["--out-dir", str(out_dir)], read_account=lambda: ACCOUNT)
    assert code == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "VST" in printed and "no order placed" in printed
    assert "equity $5,000.00, buying power $20,000.00" in printed
    assert "N = 3: $3,000.00 a name (buying power / N, at most 15% of buying power)" in printed
    assert ("pivot swing low 136.0 of 2026-09-20 (confirmed 2026-09-22) - 1 x ATR 4.6929 = 131.3071, snapped to "
            "130.99") in printed
    assert "pivot swing high 50.4 of 2026-09-22 (confirmed 2026-09-24) + 1 x ATR 2.0000 = 52.4000, snapped to 52.51" in printed
    assert "skipped OUT: in the earnings blackout" in printed
    written = list(out_dir.glob("paper_tickets_2026-09-30_*.json"))
    assert len(written) == 1
    assert [t["symbol"] for t in json.loads(written[0].read_text(encoding="utf-8"))["tickets"]] == ["VST", "SHO", "WID"]


@pytest.mark.unit
def test_cli_writes_nothing_unless_side(tmp_path, capsys, restore_meta_path):
    _write(tmp_path / "status", STATUS_NAME, _status(label="UP"))
    out_dir = tmp_path / "out"
    code = cli.main(["--as-of", "2026-09-30", "--status-dir", str(tmp_path / "status"), "--out-dir", str(out_dir)],
                    read_account=_never_read)
    assert code == cli.EXIT_OK and not out_dir.exists()
    assert "not SIDE" in capsys.readouterr().out


@pytest.mark.unit
def test_cli_writes_nothing_when_the_account_cannot_be_read(tmp_path, capsys, restore_meta_path):
    def fail():
        raise TicketInputError("cannot read the Alpaca paper account: HTTP 403")

    out_dir = tmp_path / "out"
    code = cli.main(_write_day(tmp_path) + ["--out-dir", str(out_dir)], read_account=fail)
    assert code == cli.EXIT_BAD_INPUT and not out_dir.exists()
    assert "HTTP 403" in capsys.readouterr().err


@pytest.mark.unit
def test_cli_without_the_channel_reports_writes_nothing_and_reads_no_account(tmp_path, capsys, restore_meta_path):
    args = _write_day(tmp_path)
    (tmp_path / "scanbot" / LONG_CHANNEL_NAME).unlink()
    out_dir = tmp_path / "out"
    code = cli.main(args + ["--out-dir", str(out_dir)], read_account=_never_read)
    assert code == cli.EXIT_BAD_INPUT and not out_dir.exists()
    assert "cannot read long channel report" in capsys.readouterr().err


@pytest.mark.unit
def test_the_cli_reads_the_paper_account_and_nothing_else(monkeypatch):
    calls = []

    class Reader:
        def account(self):
            calls.append("account")
            return {"equity": "5000", "buying_power": "20000", "status": "ACTIVE"}

    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(cli, "paper_reader", lambda env: Reader())
    account = cli.read_paper_account()
    assert calls == ["account"] and (account.equity, account.buying_power) == (5000.0, 20000.0)
    assert account.source == "GET https://paper-api.alpaca.markets/v2/account"


@pytest.mark.unit
def test_tickets_for_date_reads_the_account_only_on_a_side_day(tmp_path):
    _write(tmp_path, STATUS_NAME, _status(label="UP"))
    assert tickets_for_date(AS_OF, tmp_path, read_account=_never_read).tickets == []


@pytest.mark.unit
def test_cli_without_a_blackout_writes_nothing(tmp_path, capsys, restore_meta_path):
    _write(tmp_path / "status", STATUS_NAME, _status())
    out_dir = tmp_path / "out"
    code = cli.main(["--as-of", "2026-09-30", "--status-dir", str(tmp_path / "status"), "--out-dir", str(out_dir)])
    assert code == cli.EXIT_BAD_INPUT and not out_dir.exists()
    assert "no earnings blackout" in capsys.readouterr().err


@pytest.mark.unit
def test_refuses_when_trading_client_is_already_loaded(monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(sys.modules, "alpaca.trading", types.ModuleType("alpaca.trading"))
    with pytest.raises(TradingClientLoaded):
        refuse_trading_client()
    code = cli.main(["--as-of", "2026-09-30", "--status-dir", str(tmp_path)])
    assert code == cli.EXIT_BAD_INPUT and "trading client is already loaded" in capsys.readouterr().err


@pytest.mark.unit
def test_paper_tickets_load_no_alpaca_and_refuse_the_trading_client():
    """In a fresh interpreter: no alpaca or requests module is loaded, and after the refusal alpaca.trading fails."""
    code = (
        "import sys, extensions.status.paper_tickets as p, extensions.scripts.run_paper_tickets; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in ('alpaca', 'requests'))); "
        "p.refuse_trading_client()\n"
        "try:\n"
        "    import alpaca.trading.client\n"
        "    print('imported')\n"
        "except ImportError as exc:\n"
        "    print('refused' if 'refused' in str(exc) else f'other: {exc}')\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.split("\n")[:2] == ["[]", "refused"]
