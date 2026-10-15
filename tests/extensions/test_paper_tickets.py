"""Paper tickets read the newest daily status, the earnings blackout built from it, the ranked side handoff it
names and the newest pivot-confirmed channel reports for the date, by the created_at inside each file. Only the
blackout's allowed names that are passing members of that report are ticketed: a limit entry at
the channel report's support (long) or resistance (short), that same level the pivot, stop the level -/+ 1 ATR(14)
snapped to .99/.49 (long) or .01/.51 (short), target at the midline. Buying power comes from the paper
account at ticket time (a fake here). Each of the N allowed names with usable levels gets buying power / N, at most
15% of buying power, and floor(that / entry) shares whatever its stop; no name is shrunk or skipped because
another used the budget. Blackout names are listed as skipped with the reason. The TTM Squeeze gate is not run.
No label but SIDE writes anything. Alpaca's trading client is refused. No network, no orders.
"""

import json
import subprocess
import sys
import types
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from extensions.scripts import run_paper_tickets as cli
from extensions.status import squeeze_gate
from extensions.status.paper_tickets import (
    TICKET_VERSION,
    Account,
    TicketInputError,
    TradingClientLoaded,
    account_from_alpaca,
    build_ticket,
    build_tickets,
    latest_channel_report,
    name_notional_limit,
    refuse_trading_client,
    save_tickets,
    snap_stop,
    stop_for_fill,
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
TEN = {"support": 10.0, "resistance": 20.0, "atr_14": 1.0, "midline": 15.0}
ACCOUNT = Account(equity=5000.0, buying_power=20000.0, read_at=CREATED, source="fake paper account", status="ACTIVE")
# VST 138.4525 - 4.6929 = 133.7596 -> 133.49; WID 10.00 - 8.00 = 2.00 -> 1.99; SHO 50.00 + 2.00 = 52.00 -> 52.01
LEVELS = {"VST": VST, "WID": WIDE, "OUT": WIDE, "SHO": SHORTY}


def _ticket(symbol, side, levels, earnings_result=None, notional_limit=3000.0):
    line = levels.get("support" if side == "long" else "resistance")
    return build_ticket(symbol, side, line, levels.get("atr_14"), levels.get("midline"), earnings_result,
                        notional_limit=notional_limit)


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


def _report(levels_by_symbol, failed=(), created_at="2026-10-08T10:00:00+00:00", confirmed=True):
    """A pivot-confirmed channel report whose calls carry each name's support, resistance and atr_14, as the scan
    measured them. Every name is a passing member except those in failed, which keep their levels in calls."""
    keys = ("close", "support", "resistance", "atr_14")
    calls = {s: {"passed": s not in failed, "metrics": {k: lv[k] for k in keys if k in lv}}
             for s, lv in levels_by_symbol.items()}
    stages = {"at_support": len(calls), **({"pivot_confirmed": len(calls) - len(failed)} if confirmed else {})}
    return {"as_of": AS_OF.isoformat(), "created_at": created_at, "report_version": 1, "stage_counts": stages,
            "members": sorted(s for s in calls if s not in failed), "calls": calls}


def _channels(levels=None):
    levels = levels or LEVELS
    return {"long": (Path(LONG_CHANNEL_NAME), _report({s: levels[s] for s in levels if s != "SHO"})),
            "short": (Path(SHORT_CHANNEL_NAME), _report({s: levels[s] for s in levels if s == "SHO"}))}


def _build_longs(spec):
    """A SIDE day whose allowed names are these longs on TEN levels, ranked in the order given: symbol -> atr_14 in
    the long channel report, or None to leave the name out of the report."""
    symbols = list(spec)
    names = {s: _name("long", TEN, i) for i, s in enumerate(symbols, start=1)}
    channels = {"long": (Path(LONG_CHANNEL_NAME),
                         _report({s: {**TEN, "atr_14": atr} for s, atr in spec.items() if atr is not None}))}
    return _build(status=_status(long=symbols, short=()), blackout=_blackout(allowed_long=symbols, allowed_short=()),
                  handoff=_handoff(names), channels=channels)


def _build(status=None, blackout=None, handoff=None, account=ACCOUNT, channels=None):
    return build_tickets(AS_OF, (Path(STATUS_NAME), status or _status()), (Path(BLACKOUT_NAME), blackout or _blackout()),
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
def test_long_entry_is_a_limit_at_support_and_the_stop_one_atr_below_it_snapped_down_to_99_or_49():
    t = _ticket("VST", "long", VST, earnings_result="clear")
    assert (t.order_side, t.line, t.entry, t.target) == ("buy", "support", 138.45, 147.72)
    assert (t.channel_level, t.atr_14, t.stop_calculated, t.stop) == (138.4525, 4.6929, 133.7596, 133.49)
    assert t.stop_offset == t.risk_per_share == 4.96 and t.reward_per_share == 9.27 and t.reward_risk == 1.87
    assert t.quantity == 21 and t.notional_limit_dollars == 3000.0  # floor($3,000 / $138.45)
    assert t.notional == 2907.45 and t.risk_dollars == 104.16
    assert t.paper is True


@pytest.mark.unit
def test_short_entry_is_a_limit_at_resistance_and_the_stop_one_atr_above_it_snapped_up_to_01_or_51():
    t = _ticket("SHO", "short", SHORTY)
    assert (t.order_side, t.line, t.entry, t.target) == ("sell", "resistance", 50.0, 45.0)
    assert (t.channel_level, t.stop_calculated, t.stop, t.stop_offset) == (50.0, 52.0, 52.01, 2.01)
    assert t.quantity == 60 and t.notional == 3000.0 and t.risk_dollars == 120.6 and t.reward_risk == 2.49


@pytest.mark.unit
@pytest.mark.parametrize("level, stop", [
    (47.30, 46.99), (47.60, 47.49), (47.49, 47.49), (47.99, 47.99), (48.00, 47.99), (47.50, 47.49),
    (47.4899, 46.99), (133.7596, 133.49), (2.0, 1.99),
])
def test_a_long_stop_snaps_down_to_the_nearer_99_or_49_at_or_below(level, stop):
    assert snap_stop("long", level) == stop


@pytest.mark.unit
@pytest.mark.parametrize("level, stop", [
    (50.60, 51.01), (50.51, 50.51), (50.52, 51.01), (50.01, 50.01), (50.00, 50.01), (49.995, 50.01),
    (50.5001, 50.51), (52.0, 52.01),
])
def test_a_short_stop_snaps_up_to_the_nearer_01_or_51_at_or_above(level, stop):
    assert snap_stop("short", level) == stop


@pytest.mark.unit
def test_size_does_not_depend_on_the_stop_distance():
    """$3,000 at a $138.45 entry is 21 shares, whether the stop is $4.96 or $9.96 away."""
    tight, wide = _ticket("VST", "long", VST), _ticket("VST", "long", {**VST, "atr_14": 9.6929})
    assert (tight.stop_offset, tight.quantity) == (4.96, 21) and (wide.stop_offset, wide.quantity) == (9.96, 21)
    t = _ticket("WID", "long", WIDE)
    assert (t.entry, t.stop_calculated, t.stop, t.stop_offset, t.quantity, t.risk_dollars) == (
        10.0, 2.0, 1.99, 8.01, 300, 2403.0)


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
    (138.45, 133.49),  # at the entry: the ticket's stop
    (138.10, 133.14),  # a little better
    (128.00, 123.04),  # gapped below the planned stop of 133.49
])
def test_a_long_stop_follows_the_fill_and_stays_below_it(fill, stop):
    t = _ticket("VST", "long", VST)
    assert stop_for_fill("long", t.stop_offset, fill) == stop
    assert stop < fill and round(fill - stop, 2) == t.risk_per_share


@pytest.mark.unit
@pytest.mark.parametrize("fill, stop", [
    (50.00, 52.01),  # at the entry: the ticket's stop
    (50.20, 52.21),  # a little better
    (53.25, 55.26),  # gapped above the planned stop of 52.01
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
@pytest.mark.parametrize("levels, match", [
    ({**VST, "atr_14": 0}, "no usable"),
    ({k: v for k, v in VST.items() if k != "atr_14"}, "no usable"),
    ({k: v for k, v in VST.items() if k != "support"}, "no usable support or atr_14 in the long channel report"),
    ({k: v for k, v in VST.items() if k != "midline"}, "no usable"),
    ({**VST, "midline": 130.0}, "profit side"),
    ({"support": 4.0, "resistance": 6.0, "atr_14": 4.5, "midline": 5.0}, "not above zero"),
    ({**VST, "support": 20_000.0, "midline": 20_100.0}, "no whole share"),
])
def test_unusable_levels_make_no_ticket(levels, match):
    with pytest.raises(ValueError, match=match):
        _ticket("VST", "long", levels)


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
    assert (by["VST"].quantity, by["VST"].notional, by["VST"].risk_dollars) == (21, 2907.45, 104.16)
    assert (by["SHO"].quantity, by["SHO"].notional, by["SHO"].risk_dollars) == (60, 3000.0, 120.6)
    assert (by["WID"].quantity, by["WID"].notional, by["WID"].risk_dollars) == (300, 3000.0, 2403.0)
    assert result.total_notional == 8907.45 and result.total_risk == 2627.76
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
def test_entry_and_stop_come_from_the_channel_report_not_the_handoff_or_the_close():
    """The handoff's support and the last close differ from the long channel report's support; the report wins."""
    handoff = _handoff()
    handoff["names"]["VST"] = _name("long", {**VST, "support": 139.9, "atr_14": 1.0, "close": 141.2}, 1)
    result = _build(handoff=handoff, channels=_channels({**LEVELS, "VST": {**VST, "close": 141.2}}))
    t = next(t for t in result.tickets if t.symbol == "VST")
    assert (t.entry, t.channel_level, t.atr_14, t.stop_calculated, t.stop) == (138.45, 138.4525, 4.6929, 133.7596, 133.49)


@pytest.mark.unit
def test_a_name_missing_from_the_channel_report_is_skipped_not_ticketed():
    result = _build(channels=_channels({k: v for k, v in LEVELS.items() if k != "WID"}))
    assert [t.symbol for t in result.tickets] == ["VST", "SHO"]
    assert result.skipped["WID"] == f"not a member of the newest confirmed long channel report {LONG_CHANNEL_NAME}"
    assert result.rules["names_n"] == 2 and all(t.notional_limit_dollars == 3000.0 for t in result.tickets)


@pytest.mark.unit
def test_a_name_that_failed_confirmation_is_skipped_though_its_levels_are_in_the_report():
    """A failed call still records support and atr_14; the name is not a member, so it gets no ticket and no share
    of buying power."""
    channels = _channels()
    channels["long"] = (Path(LONG_CHANNEL_NAME), _report({s: LEVELS[s] for s in ("VST", "WID", "OUT")}, failed=("WID",)))
    result = _build(channels=channels)
    assert [t.symbol for t in result.tickets] == ["VST", "SHO"]
    assert result.skipped["WID"].startswith("not a member of the newest confirmed long channel report")
    assert result.rules["names_n"] == 2


@pytest.mark.unit
def test_a_member_whose_call_did_not_pass_is_skipped():
    channels = _channels()
    channels["long"][1]["calls"]["WID"]["passed"] = False
    result = _build(channels=channels)
    assert "WID" not in {t.symbol for t in result.tickets} and "not a member" in result.skipped["WID"]


@pytest.mark.unit
def test_a_handoff_ranked_from_another_channel_report_is_warned():
    handoff = _handoff()
    handoff["long_channel_report"] = "C:\\elsewhere\\long_channel_2026-09-30_20261007T100000Z.json"
    result = _build(handoff=handoff)
    assert ("the ranked side handoff ranked from long channel report long_channel_2026-09-30_20261007T100000Z.json; "
            f"membership and levels are from the newer {LONG_CHANNEL_NAME}") in result.warnings
    assert not any("short channel report" in w for w in result.warnings)


@pytest.mark.unit
def test_the_newest_channel_report_is_chosen_by_the_created_at_inside_the_file(tmp_path):
    """The file whose name sorts last was created first; the created_at inside the file decides, not the name."""
    newest = _report({"VST": VST}, created_at="2026-10-09T02:00:00+00:00")
    _write(tmp_path, "long_channel_2026-09-30_20261001T000000Z.json", newest)
    _write(tmp_path, "long_channel_2026-09-30_20261009T000000Z.json",
           _report({"VST": VST, "WID": WIDE}, created_at="2026-10-08T00:00:00+00:00"))
    _write(tmp_path, "long_channel_2026-09-29_20261010T000000Z.json",
           {**_report({"VST": VST}, created_at="2026-10-10T00:00:00+00:00"), "as_of": "2026-09-29"})
    path, data = latest_channel_report(AS_OF, "long", tmp_path)
    assert path.name == "long_channel_2026-09-30_20261001T000000Z.json" and data["members"] == ["VST"]


@pytest.mark.unit
def test_a_newest_channel_report_without_pivot_confirmation_stops_with_no_fallback(tmp_path):
    _write(tmp_path, "long_channel_2026-09-30_20261008T010000Z.json",
           _report({"VST": VST}, created_at="2026-10-08T01:00:00+00:00"))
    _write(tmp_path, "long_channel_2026-09-30_20261008T020000Z.json",
           _report({"VST": VST, "WID": WIDE}, created_at="2026-10-08T02:00:00+00:00", confirmed=False))
    with pytest.raises(TicketInputError, match="has no pivot_confirmed stage"):
        latest_channel_report(AS_OF, "long", tmp_path)


@pytest.mark.unit
def test_an_unreadable_channel_report_stops(tmp_path):
    _write(tmp_path, "long_channel_2026-09-30_20261008T010000Z.json", _report({"VST": VST}))
    (tmp_path / "long_channel_2026-09-30_20261008T020000Z.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(TicketInputError, match="cannot read long channel report"):
        latest_channel_report(AS_OF, "long", tmp_path)
    (tmp_path / "long_channel_2026-09-30_20261008T020000Z.json").write_text(json.dumps({"as_of": "2026-09-30"}),
                                                                          encoding="utf-8")
    with pytest.raises(TicketInputError, match="cannot read long channel report"):
        latest_channel_report(AS_OF, "long", tmp_path)


@pytest.mark.unit
def test_the_day_tickets_only_members_of_the_newest_confirmed_report(tmp_path):
    """The handoff names an older unconfirmed long report in which WID passes; the newest report, confirmed, drops
    WID, so WID gets no ticket."""
    _write_day(tmp_path)
    _write(tmp_path / "scanbot", "long_channel_2026-09-30_20261008T200000Z.json",
           _report({s: LEVELS[s] for s in ("VST", "WID", "OUT")}, failed=("WID",),
                   created_at="2026-10-08T20:00:00+00:00"))
    result = tickets_for_date(AS_OF, tmp_path / "status", tmp_path / "handoffs", lambda: ACCOUNT, tmp_path / "scanbot")
    assert [t.symbol for t in result.tickets] == ["VST", "SHO"]
    assert "not a member of the newest confirmed long channel report" in result.skipped["WID"]
    assert result.sources["long_channel_report"].endswith("long_channel_2026-09-30_20261008T200000Z.json")
    assert result.sources["short_channel_report"].endswith(SHORT_CHANNEL_NAME)


@pytest.mark.unit
def test_tickets_are_listed_in_rank_order_not_list_order():
    result = _build(blackout=_blackout(allowed_long=("WID", "VST")))
    assert [(t.symbol, t.rank) for t in result.tickets] == [("VST", 1), ("SHO", 1), ("WID", 2)]


@pytest.mark.unit
def test_every_name_gets_the_same_capital_however_the_stops_differ():
    """N = 7, $20,000 / 7 = $2,857.14 a name (under the 15% cap): 285 shares at $10 each, though E's stop is 7.49 and
    the rest 8.99."""
    result = _build_longs({"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0, "E": 2.4, "F": 1.0, "G": 1.0})
    assert [(t.symbol, t.stop, t.quantity) for t in result.tickets] == [
        ("A", 8.99, 285), ("B", 8.99, 285), ("C", 8.99, 285), ("D", 8.99, 285), ("E", 7.49, 285), ("F", 8.99, 285),
        ("G", 8.99, 285)]
    assert result.rules["name_notional_limit"] == 2857.14 and result.total_notional == 19950.0
    assert list(result.skipped) == ["OUT"]  # only the blackout name


@pytest.mark.unit
def test_a_name_with_no_channel_levels_is_skipped_and_not_counted_in_n():
    result = _build_longs({"A": 1.0, "B": None})
    assert [(t.symbol, t.quantity, t.notional_limit_dollars) for t in result.tickets] == [("A", 300, 3000.0)]
    assert result.rules["names_n"] == 1
    assert result.skipped["B"].startswith("not a member of the newest confirmed long channel report")


@pytest.mark.unit
def test_a_member_with_no_channel_levels_is_skipped_and_not_counted_in_n():
    channels = _channels()
    channels["long"][1]["calls"]["WID"]["metrics"] = {}
    result = _build(channels=channels)
    assert result.rules["names_n"] == 2
    assert result.skipped["WID"].startswith("no usable support or atr_14 in the long channel report")


@pytest.mark.unit
def test_the_ticket_gate_never_calls_the_squeeze_gate_and_reports_no_squeeze(monkeypatch, tmp_path):
    """Every allowed name with usable levels is ticketed; the squeeze gate is not called and nothing in the report
    names it."""
    def never(*args, **kwargs):
        raise AssertionError("the squeeze gate was called")

    monkeypatch.setattr(squeeze_gate, "squeeze_gates", never)
    monkeypatch.setattr(squeeze_gate, "rules", never)
    _write_day(tmp_path)
    result = tickets_for_date(AS_OF, tmp_path / "status", tmp_path / "handoffs", lambda: ACCOUNT, tmp_path / "scanbot")
    assert [t.symbol for t in result.tickets] == ["VST", "SHO", "WID"] and list(result.skipped) == ["OUT"]
    data = result.to_dict()
    assert "squeeze" not in json.dumps([data["tickets"], data["rules"], data["skipped"], data["warnings"]]).lower()


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
    assert data["ticket_version"] == TICKET_VERSION == 8
    assert "squeeze" not in data["tickets"][0]
    first = data["tickets"][0]
    assert (first["entry"], first["channel_level"], first["atr_14"], first["stop_calculated"], first["stop"],
            first["stop_offset"]) == (138.45, 138.4525, 4.6929, 133.7596, 133.49, 4.96)
    assert "pivot" not in first and "line_level" not in first
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
    assert "squeeze" not in printed.lower()
    assert "VST" in printed and "no order placed" in printed
    assert "equity $5,000.00, buying power $20,000.00" in printed
    assert "N = 3: $3,000.00 a name (buying power / N, at most 15% of buying power)" in printed
    assert "limit    138.45" in printed
    assert "channel support 138.4525 - 1 x ATR 4.6929 = 133.7596, snapped to 133.49" in printed
    assert "channel resistance 50.0 + 1 x ATR 2.0000 = 52.0000, snapped to 52.01" in printed
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
    assert "no long channel report for 2026-09-30" in capsys.readouterr().err


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
