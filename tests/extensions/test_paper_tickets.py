"""Paper tickets read the newest daily status, the earnings blackout built from it and the ranked side handoff
it names. Only the blackout's allowed names are ticketed: entry at the channel line, stop 0.25 ATR beyond it,
target at the midline, size the smaller of $100 risk (2% of $5,000 equity) and $5,000 notional (25% of $20,000
buying power). Names are walked in rank order while the book stays at or under $20,000 notional and $400 risk.
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
from extensions.status.paper_tickets import (
    BOOK_NOTIONAL,
    BOOK_RISK,
    NOTIONAL,
    RISK,
    TicketInputError,
    TradingClientLoaded,
    build_ticket,
    build_tickets,
    refuse_trading_client,
    save_tickets,
)

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
STATUS_NAME = "daily_status_2026-09-30_20261008T231343Z.json"
BLACKOUT_NAME = "earnings_blackout_2026-09-30_20261008T232454Z.json"
HANDOFF_NAME = "ranked_side_handoff_2026-09-30_20261008T110409Z.json"
VST = {"support": 138.4525, "resistance": 156.99, "atr_14": 4.6929, "midline": 147.7212}
WIDE = {"support": 10.0, "resistance": 20.0, "atr_14": 8.0, "midline": 15.0}
SHORTY = {"support": 40.0, "resistance": 50.0, "atr_14": 2.0, "midline": 45.0}
FULL = {"support": 100.0, "resistance": 120.0, "atr_14": 0.4, "midline": 110.0}  # $5,000 notional, $5 risk
RISKY = WIDE  # $500 notional, $100 risk


def _status(label="SIDE", long=("VST", "WID", "OUT"), short=("SHO",), blocked=False):
    return {"as_of": AS_OF.isoformat(), "created_at": "2026-10-08T23:13:43+00:00", "market_label": label,
            "channel_names_blocked": blocked, "confirmed": {"long": list(long), "short": list(short)},
            "sources": {"ranked_side_handoff": f"C:\\elsewhere\\{HANDOFF_NAME}"},
            "orders_placed": False, "status_version": 1}


def _blackout(allowed_long=("VST", "WID"), allowed_short=("SHO",), status_file=STATUS_NAME, label="SIDE"):
    names = [{"symbol": "VST", "result": "clear"}, {"symbol": "WID", "result": "unknown_kept"},
             {"symbol": "OUT", "result": "removed"}, {"symbol": "SHO", "result": "flagged_kept"}]
    return {"as_of": AS_OF.isoformat(), "created_at": "2026-10-08T23:24:54+00:00",
            "daily_status_file": f"C:\\elsewhere\\{status_file}", "market_label": label,
            "allowed": {"long": list(allowed_long), "short": list(allowed_short)}, "removed": ["OUT"],
            "names": names, "warnings": ["estimated dates"], "orders_placed": False, "report_version": 1}


def _name(side, levels, rank):
    return {"side": side, "setup": "LONG_FADE" if side == "long" else "SHORT_FADE", "status": "PLAN",
            "rank": rank, "levels": levels}


def _handoff(names=None):
    return {"as_of": AS_OF.isoformat(), "created_at": "2026-10-08T11:04:09+00:00",
            "names": names or {"VST": _name("long", VST, 1), "WID": _name("long", WIDE, 2),
                               "OUT": _name("long", WIDE, 3), "SHO": _name("short", SHORTY, 1)}}


def _build_longs(levels_by_symbol):
    """A SIDE day whose allowed names are these longs, ranked in the order given."""
    symbols = list(levels_by_symbol)
    names = {s: _name("long", lv, i) for i, (s, lv) in enumerate(levels_by_symbol.items(), start=1)}
    return _build(status=_status(long=symbols, short=()), blackout=_blackout(allowed_long=symbols, allowed_short=()),
                  handoff=_handoff(names))


def _build(status=None, blackout=None, handoff=None):
    return build_tickets(AS_OF, (Path(STATUS_NAME), status or _status()),
                         (Path(BLACKOUT_NAME), blackout or _blackout()),
                         (Path(HANDOFF_NAME), handoff or _handoff()), created_at=CREATED)


def _write(folder, name, data):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def restore_meta_path(monkeypatch):
    """cli.main installs the import refusal; keep it out of the rest of the test session.

    Other test modules may have loaded alpaca.trading in this session; hide it for the test so the CLI runs.
    """
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    for name in [m for m in sys.modules if m == "alpaca.trading" or m.startswith("alpaca.trading.")]:
        monkeypatch.delitem(sys.modules, name)


@pytest.mark.unit
def test_long_ticket_is_bound_by_notional():
    t = build_ticket("VST", "long", VST, "clear")
    assert (t.order_side, t.entry, t.stop, t.target) == ("buy", 138.45, 137.28, 147.72)
    assert t.risk_per_share == 1.17 and t.reward_per_share == 9.27
    assert t.quantity_by_risk == 85 and t.quantity_by_notional == 36
    assert t.quantity == 36 and t.size_bound_by == NOTIONAL
    assert t.risk_limit_dollars == 100.0 and t.notional_limit_dollars == 5000.0
    assert t.notional == 4984.2 and t.risk_dollars == 42.12 and t.paper is True


@pytest.mark.unit
def test_long_ticket_is_bound_by_risk():
    t = build_ticket("WID", "long", WIDE)
    assert (t.entry, t.stop, t.target) == (10.0, 8.0, 15.0)
    assert t.quantity_by_risk == 50 and t.quantity_by_notional == 500
    assert t.quantity == 50 and t.size_bound_by == RISK and t.risk_dollars == 100.0 and t.reward_risk == 2.5


@pytest.mark.unit
def test_short_ticket_stops_above_resistance_and_targets_midline():
    t = build_ticket("SHO", "short", SHORTY)
    assert (t.order_side, t.line, t.entry, t.stop, t.target) == ("sell", "resistance", 50.0, 50.5, 45.0)
    assert t.quantity_by_risk == 200 and t.quantity_by_notional == 100
    assert t.quantity == 100 and t.size_bound_by == NOTIONAL and t.reward_risk == 10.0


@pytest.mark.unit
@pytest.mark.parametrize("levels, match", [
    ({**VST, "atr_14": 0}, "no usable"),
    ({k: v for k, v in VST.items() if k != "midline"}, "no usable"),
    ({**VST, "midline": 130.0}, "profit side"),
    ({**VST, "support": 20_000.0, "midline": 20_100.0}, "no whole share"),
])
def test_unusable_levels_make_no_ticket(levels, match):
    with pytest.raises(ValueError, match=match):
        build_ticket("VST", "long", levels)


@pytest.mark.unit
def test_only_allowed_names_are_ticketed_and_flags_carry_over():
    result = _build()
    assert [(t.symbol, t.side) for t in result.tickets] == [("VST", "long"), ("SHO", "short"), ("WID", "long")]
    assert "OUT" not in {t.symbol for t in result.tickets}  # removed by the blackout
    assert result.total_notional == 10484.2 and result.total_risk == 192.12 and result.not_ticketed == {}
    assert {t.symbol: t.earnings_result for t in result.tickets} == {
        "VST": "clear", "WID": "unknown_kept", "SHO": "flagged_kept"}
    assert "WID: earnings result unknown_kept" in result.warnings and "estimated dates" in result.warnings
    assert result.orders_placed is False and result.paper is True and result.no_ticket_reason is None


@pytest.mark.unit
def test_a_name_with_bad_levels_is_skipped_not_ticketed():
    handoff = _handoff()
    handoff["names"]["WID"]["levels"] = {**WIDE, "atr_14": None}
    result = _build(handoff=handoff)
    assert [t.symbol for t in result.tickets] == ["VST", "SHO"]
    assert "no usable" in result.skipped["WID"]


@pytest.mark.unit
def test_walk_follows_rank_not_list_order():
    result = _build(blackout=_blackout(allowed_long=("WID", "VST")))
    assert [(t.symbol, t.rank) for t in result.tickets] == [("VST", 1), ("SHO", 1), ("WID", 2)]


@pytest.mark.unit
def test_walk_stops_at_book_notional_and_lists_the_rest():
    result = _build_longs({"A": FULL, "B": FULL, "C": FULL, "D": FULL, "E": FULL, "F": RISKY})
    assert [t.symbol for t in result.tickets] == ["A", "B", "C", "D"]
    assert result.total_notional == 20000.0 and result.total_risk == 20.0  # at the limit is allowed
    assert list(result.not_ticketed) == ["E", "F"]
    assert {n["stopped_by"] for n in result.not_ticketed.values()} == {BOOK_NOTIONAL}
    assert result.not_ticketed["E"] == {"side": "long", "rank": 5, "stopped_by": BOOK_NOTIONAL,
                                        "quantity": 50, "notional": 5000.0, "risk_dollars": 5.0}
    assert all(t.size_bound_by == NOTIONAL for t in result.tickets)


@pytest.mark.unit
def test_walk_stops_at_book_risk_and_does_not_skip_ahead():
    result = _build_longs({"A": RISKY, "B": RISKY, "C": RISKY, "D": RISKY, "E": RISKY, "F": FULL})
    assert [t.symbol for t in result.tickets] == ["A", "B", "C", "D"]
    assert result.total_risk == 400.0 and result.total_notional == 2000.0
    # F alone would fit the $400 risk book ($5), but the walk stopped at E
    assert list(result.not_ticketed) == ["E", "F"]
    assert {n["stopped_by"] for n in result.not_ticketed.values()} == {BOOK_RISK}
    assert all(t.size_bound_by == RISK for t in result.tickets)


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
    assert data["tickets"][0]["size_bound_by"] == NOTIONAL
    assert data["rules"]["name_notional_pct_of_buying_power"] == 0.25 and data["rules"]["risk_pct_of_equity"] == 0.02
    assert data["rules"]["book_notional_limit"] == 20000.0 and data["rules"]["book_risk_limit"] == 400.0
    assert data["total_notional"] == 10484.2 and data["not_ticketed"] == {}
    with pytest.raises(FileExistsError):
        save_tickets(result, tmp_path)


@pytest.mark.unit
def test_cli_prints_and_writes(tmp_path, capsys, restore_meta_path):
    _write(tmp_path / "status", STATUS_NAME, _status())
    _write(tmp_path / "status", BLACKOUT_NAME, _blackout())
    _write(tmp_path / "handoffs", HANDOFF_NAME, _handoff())
    out_dir = tmp_path / "out"
    code = cli.main(["--as-of", "2026-09-30", "--status-dir", str(tmp_path / "status"),
                     "--handoff-dir", str(tmp_path / "handoffs"), "--out-dir", str(out_dir)])
    assert code == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "VST" in printed and "bound by notional" in printed and "no order placed" in printed
    written = list(out_dir.glob("paper_tickets_2026-09-30_*.json"))
    assert len(written) == 1
    assert [t["symbol"] for t in json.loads(written[0].read_text(encoding="utf-8"))["tickets"]] == ["VST", "SHO", "WID"]


@pytest.mark.unit
def test_cli_writes_nothing_unless_side(tmp_path, capsys, restore_meta_path):
    _write(tmp_path / "status", STATUS_NAME, _status(label="UP"))
    out_dir = tmp_path / "out"
    code = cli.main(["--as-of", "2026-09-30", "--status-dir", str(tmp_path / "status"), "--out-dir", str(out_dir)])
    assert code == cli.EXIT_OK and not out_dir.exists()
    assert "not SIDE" in capsys.readouterr().out


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
