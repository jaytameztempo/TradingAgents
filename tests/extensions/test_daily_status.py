"""Daily status reads the newest SPY+QQQ route, ranked side handoff and side 4-hour confirmation for a
date. Bots follow the route; confirmed names are listed only under SIDE when the three files chain
together, and are blocked otherwise. One status JSON is written and never overwritten. No network,
no Alpaca module, no orders.
"""

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from extensions.scripts import run_daily_status as cli
from extensions.status.daily_status import (
    DOWN_BOT,
    LONG,
    SHORT,
    SIDE_BOT,
    UP_BOT,
    StatusInputError,
    build_status,
    latest_route,
    save_status,
    status_for_date,
)

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTE_NAME = "route_ALL_2026-09-30_20261004T010554Z.json"
HANDOFF_NAME = "ranked_side_handoff_2026-09-30_20261008T110409Z.json"
CONFIRM_NAME = "side_confirm4h_2026-09-30_20261008T111320Z.json"


def _route(label="SIDE", symbol="SPY+QQQ", created="2026-10-04T01:05:54+00:00", tradeable=()):
    return {"as_of": AS_OF.isoformat(), "created_at": created, "series": "ALL", "regime_symbol": symbol,
            "regime_label": label, "up_allowed": label == "UP", "down_allowed": label == "DOWN",
            "tradeable": list(tradeable), "blocked": [], "reason": f"market is {label}", "schema_version": 1,
            "regime_labels": {"SPY": label, "QQQ": label}}


def _handoff(label="SIDE", route_file=ROUTE_NAME):
    return {"as_of": AS_OF.isoformat(), "created_at": "2026-10-08T11:04:09+00:00", "route_file": route_file,
            "regime_label": label, "planned_long": ["AAA", "BBB"], "planned_short": ["ZZZ"],
            "names": {}, "warnings": ["w1"], "orders_placed": False}


def _confirm(handoff_file=HANDOFF_NAME, confirmed=None):
    return {"as_of": AS_OF.isoformat(), "created_at": "2026-10-08T11:13:20+00:00",
            "ranked_handoff": handoff_file, "ranked_handoff_created_at": "2026-10-08T11:04:09+00:00",
            "regime_label": "SIDE", "confirmed": confirmed or {LONG: ["AAA"], SHORT: ["ZZZ"]},
            "warnings": ["w1", "w2"], "orders_placed": False, "report_version": 1}


def _found(name, data):
    return Path(name), data


def _write(folder, name, data):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(data), encoding="utf-8")


def _dirs(tmp_path, route=None, handoff=None, confirm=None):
    dirs = {k: tmp_path / k for k in ("routes", "handoffs", "scanbot")}
    for d in dirs.values():
        d.mkdir()
    _write(dirs["routes"], ROUTE_NAME, route or _route())
    if handoff is not False:
        _write(dirs["handoffs"], HANDOFF_NAME, handoff or _handoff())
    if confirm is not False:
        _write(dirs["scanbot"], CONFIRM_NAME, confirm or _confirm())
    return dirs


@pytest.mark.unit
def test_side_lists_the_confirmed_names():
    status = build_status(AS_OF, _found(ROUTE_NAME, _route()), _found(HANDOFF_NAME, _handoff()),
                          _found(CONFIRM_NAME, _confirm()), created_at=CREATED)
    assert status.market_label == "SIDE"
    assert status.bots == {UP_BOT: False, DOWN_BOT: False, SIDE_BOT: True}
    assert status.channel_names_blocked is False and status.blocked_reasons == []
    assert status.confirmed == {LONG: ["AAA"], SHORT: ["ZZZ"]}
    assert status.trend_names == [] and status.orders_placed is False
    assert status.warnings == ["w1", "w2"]


@pytest.mark.unit
@pytest.mark.parametrize("label, allowed", [("UP", UP_BOT), ("DOWN", DOWN_BOT)])
def test_a_trend_label_blocks_the_channel_names(label, allowed):
    route = _found(ROUTE_NAME, _route(label, tradeable=["MSFT"]))
    status = build_status(AS_OF, route, _found(HANDOFF_NAME, _handoff(label)), _found(CONFIRM_NAME, _confirm()))
    assert status.bots == {UP_BOT: allowed == UP_BOT, DOWN_BOT: allowed == DOWN_BOT, SIDE_BOT: False}
    assert status.trend_names == ["MSFT"]
    assert status.channel_names_blocked is True
    assert status.confirmed == {LONG: [], SHORT: []}
    assert status.blocked_reasons[0] == f"market label is {label}, not SIDE; channel names are blocked"


@pytest.mark.unit
def test_a_confirmation_of_another_handoff_blocks():
    stale = _confirm(handoff_file="ranked_side_handoff_2026-09-30_20261008T100000Z.json")
    status = build_status(AS_OF, _found(ROUTE_NAME, _route()), _found(HANDOFF_NAME, _handoff()),
                          _found(CONFIRM_NAME, stale))
    assert status.channel_names_blocked and status.confirmed == {LONG: [], SHORT: []}
    assert "not the newest ranked side handoff" in status.blocked_reasons[0]


@pytest.mark.unit
def test_a_handoff_from_another_route_blocks():
    handoff = _handoff(route_file="route_ALL_2026-09-30_20261003T012319Z.json")
    status = build_status(AS_OF, _found(ROUTE_NAME, _route()), _found(HANDOFF_NAME, handoff),
                          _found(CONFIRM_NAME, _confirm()))
    assert status.channel_names_blocked and "built from route" in status.blocked_reasons[0]


@pytest.mark.unit
def test_a_confirmed_name_that_was_not_planned_blocks():
    confirm = _confirm(confirmed={LONG: ["AAA", "QQQ"], SHORT: []})
    status = build_status(AS_OF, _found(ROUTE_NAME, _route()), _found(HANDOFF_NAME, _handoff()),
                          _found(CONFIRM_NAME, confirm))
    assert status.channel_names_blocked and status.blocked_reasons == [
        "confirmed long names not planned in the ranked side handoff: QQQ"]


@pytest.mark.unit
def test_a_missing_handoff_or_confirmation_blocks_but_still_reports(tmp_path):
    dirs = _dirs(tmp_path, handoff=False, confirm=False)
    status = status_for_date(AS_OF, dirs["routes"], dirs["handoffs"], dirs["scanbot"])
    assert status.bots[SIDE_BOT] is True and status.channel_names_blocked
    assert status.blocked_reasons == [f"no ranked side handoff for {AS_OF}", f"no side 4-hour confirmation for {AS_OF}"]
    assert status.sources["ranked_side_handoff"] is None and status.sources["side_confirm4h"] is None


@pytest.mark.unit
def test_a_newer_spy_only_route_is_ignored(tmp_path):
    dirs = _dirs(tmp_path)
    _write(dirs["routes"], "route_UP_2026-09-30_20261009T000000Z.json",
           _route("UP", symbol="SPY", created="2026-10-09T00:00:00+00:00"))
    path, route = latest_route(AS_OF, dirs["routes"])
    assert path.name == ROUTE_NAME and route["regime_label"] == "SIDE"
    status = status_for_date(AS_OF, dirs["routes"], dirs["handoffs"], dirs["scanbot"])
    assert status.confirmed == {LONG: ["AAA"], SHORT: ["ZZZ"]}


@pytest.mark.unit
def test_the_newest_spy_qqq_route_wins_by_created_at(tmp_path):
    dirs = _dirs(tmp_path)
    _write(dirs["routes"], "route_ALL_2026-09-30_00000000T000000Z.json",
           _route("UP", created="2026-10-09T00:00:00+00:00"))
    path, route = latest_route(AS_OF, dirs["routes"])
    assert route["regime_label"] == "UP" and path.name.startswith("route_ALL_2026-09-30_0000")


@pytest.mark.unit
def test_no_route_or_a_bad_route_stops(tmp_path):
    with pytest.raises(StatusInputError, match="no SPY\\+QQQ route"):
        latest_route(AS_OF, tmp_path)
    bad = _route("SIDE")
    bad["up_allowed"] = True
    _write(tmp_path, ROUTE_NAME, bad)
    with pytest.raises(StatusInputError, match="up_allowed=True"):
        latest_route(AS_OF, tmp_path)
    (tmp_path / "route_ALL_2026-09-30_x.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(StatusInputError, match="cannot read"):
        latest_route(AS_OF, tmp_path)


@pytest.mark.unit
def test_save_never_overwrites(tmp_path):
    status = build_status(AS_OF, _found(ROUTE_NAME, _route()), created_at=CREATED)
    path = save_status(status, tmp_path)
    assert path.name == "daily_status_2026-09-30_20261008T120000Z.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["as_of"] == "2026-09-30" and data["orders_placed"] is False
    with pytest.raises(FileExistsError):
        save_status(status, tmp_path)


@pytest.mark.unit
def test_cli_prints_and_writes_one_status(tmp_path, capsys):
    dirs = _dirs(tmp_path)
    out_dir = tmp_path / "status"
    code = cli.main(["--as-of", "2026-09-30", "--route-dir", str(dirs["routes"]), "--handoff-dir",
                     str(dirs["handoffs"]), "--report-dir", str(dirs["scanbot"]), "--out-dir", str(out_dir)])
    assert code == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "market label: SIDE" in printed and "UPBot:   BLOCKED" in printed and "SIDEBot: ALLOWED" in printed
    assert "confirmed long:  AAA" in printed and "no order placed" in printed
    assert len(list(out_dir.glob("daily_status_2026-09-30_*.json"))) == 1


@pytest.mark.unit
def test_cli_without_a_route_writes_nothing(tmp_path, capsys):
    out_dir = tmp_path / "status"
    code = cli.main(["--as-of", "2026-09-30", "--route-dir", str(tmp_path), "--out-dir", str(out_dir)])
    assert code == cli.EXIT_BAD_INPUT and not out_dir.exists()
    assert "no status written" in capsys.readouterr().err


@pytest.mark.unit
def test_daily_status_imports_no_alpaca_module():
    """In a fresh interpreter, loading the status module and its script leaves no alpaca module loaded."""
    code = (
        "import sys, extensions.status.daily_status, extensions.scripts.run_daily_status; "
        "print(sorted(m for m in sys.modules if m == 'alpaca' or m.startswith('alpaca.')))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"
