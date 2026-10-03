"""The router allows UPBot only under UP and DOWNBot only under DOWN, routes SIDE names to SIDEBot,
and makes no decision without the regime label and the allowed side's basket.

Baskets and labels are written to temporary folders; nothing fetches bars.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from extensions.regime import regime_bot
from extensions.regime.regime_bot import DOWN, SIDE, UP, RegimeLabel, save_label
from extensions.router.strategy_router import (
    MissingInput,
    RouteDecision,
    latest_basket,
    latest_regime,
    load_decision,
    route,
    route_for_date,
    save_decision,
)
from extensions.scans import breakdown_short_candidates, upward_trend_momentum
from extensions.scans.basket import BasketMember, Rejection, TickerBasket, save_basket
from extensions.scripts import run_down_bot, run_side_bot, run_up_bot
from extensions.scripts import run_router as cli

AS_OF = date(2026, 9, 30)
T0 = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)
UP_SCAN = upward_trend_momentum.SCAN_NAME
DOWN_SCAN = breakdown_short_candidates.SCAN_NAME


@pytest.fixture(autouse=True)
def _no_fetching(monkeypatch):
    """The router reads files only; any bar fetch is a bug."""
    for module in (upward_trend_momentum, breakdown_short_candidates, regime_bot):
        monkeypatch.setattr(module, "fetch_daily_bars", lambda *a, **k: pytest.fail("fetched bars"))


def _basket(tickers=("QQQ", "SPY"), as_of=AS_OF, created_at=T0, scan_name=UP_SCAN):
    return TickerBasket(
        scan_name=scan_name,
        as_of=as_of,
        created_at=created_at,
        universe=[*tickers, "AAPL"],
        parameters={},
        members=[BasketMember(t, 0.01, {}) for t in tickers],
        rejected=[Rejection("AAPL", "no bars")],
    )


def _down_basket(tickers=("NVDA",), **kwargs):
    return _basket(tickers, scan_name=DOWN_SCAN, **kwargs)


def _label(label=UP, symbol="SPY", as_of=AS_OF, created_at=T0):
    return RegimeLabel(
        symbol=symbol, as_of=as_of, created_at=created_at, label=label, reasons=[], metrics={}, parameters={}
    )


def _route(label, up=True, down=True):
    return route(
        _label(label), "r.json",
        up=("up.json", _basket()) if up else None,
        down=("down.json", _down_basket()) if down else None,
        created_at=T0,
    )


@pytest.fixture
def dirs(tmp_path):
    baskets, regimes, routes = tmp_path / "baskets", tmp_path / "regimes", tmp_path / "routes"
    baskets.mkdir()
    regimes.mkdir()
    return baskets, regimes, routes


@pytest.mark.unit
def test_up_allows_upbot_for_upward_passers_only():
    decision = _route(UP)
    assert decision.up_allowed and not decision.down_allowed
    assert decision.tradeable == ["QQQ", "SPY"]
    assert decision.blocked == []
    assert decision.up_passed == ["QQQ", "SPY"] and decision.down_passed == ["NVDA"]
    assert "UPBot is allowed" in decision.reason and "DOWNBot is blocked" in decision.reason


@pytest.mark.unit
def test_down_allows_downbot_for_breakdown_passers_only():
    decision = _route(DOWN)
    assert decision.down_allowed and not decision.up_allowed
    assert decision.tradeable == ["NVDA"]
    assert decision.blocked == []
    assert decision.up_passed == ["QQQ", "SPY"] and decision.down_passed == ["NVDA"]
    assert "DOWNBot is allowed" in decision.reason and "UPBot is blocked" in decision.reason


@pytest.mark.unit
def test_side_blocks_both_and_routes_every_passer_to_sidebot():
    decision = _route(SIDE)
    assert not decision.up_allowed and not decision.down_allowed
    assert decision.tradeable == []
    assert decision.blocked == ["QQQ", "SPY", "NVDA"]
    assert "UPBot and DOWNBot are blocked" in decision.reason


@pytest.mark.unit
def test_rejected_tickers_are_never_routed():
    for label in (UP, DOWN, SIDE):
        decision = _route(label)
        assert "AAPL" not in decision.tradeable + decision.blocked + decision.up_passed + decision.down_passed


@pytest.mark.unit
@pytest.mark.parametrize("label, missing, scan", [(UP, "up", UP_SCAN), (DOWN, "down", DOWN_SCAN)])
def test_a_missing_allowed_side_basket_stops_the_route(label, missing, scan):
    with pytest.raises(MissingInput, match=f"no {scan} basket for 2026-09-30; it is required when the SPY regime is {label}"):
        _route(label, up=missing != "up", down=missing != "down")


@pytest.mark.unit
def test_a_missing_other_side_basket_is_recorded_not_fatal():
    up = _route(UP, down=False)
    assert up.tradeable == ["QQQ", "SPY"] and up.down_passed == [] and up.down_basket_file is None

    down = _route(DOWN, up=False)
    assert down.tradeable == ["NVDA"] and down.up_passed == [] and down.basket_file is None


@pytest.mark.unit
def test_side_needs_no_basket():
    decision = _route(SIDE, up=False, down=False)
    assert decision.tradeable == [] and decision.blocked == []
    assert decision.basket_file is None and decision.down_basket_file is None


@pytest.mark.unit
def test_a_basket_and_label_for_different_dates_are_refused():
    with pytest.raises(ValueError, match="regime label is for"):
        route(_label(UP, as_of=AS_OF - timedelta(days=1)), "r.json", up=("b.json", _basket()))


@pytest.mark.unit
def test_the_newest_basket_of_each_scan_and_label_for_the_date_win(dirs):
    baskets, regimes, _ = dirs
    save_basket(_basket(("SPY",), created_at=T0), baskets)
    save_basket(_basket(("NVDA",), created_at=T0 + timedelta(hours=1)), baskets)
    save_basket(_basket(("MSFT",), created_at=T0 + timedelta(hours=2), as_of=AS_OF - timedelta(days=1)), baskets)
    save_basket(_down_basket(("AAPL",), created_at=T0 + timedelta(hours=3)), baskets)
    save_basket(_down_basket(("QQQ",), created_at=T0 + timedelta(hours=4)), baskets)
    save_label(_label(SIDE, created_at=T0), regimes)
    save_label(_label(UP, created_at=T0 + timedelta(hours=1)), regimes)
    save_label(_label(DOWN, created_at=T0 + timedelta(hours=2), symbol="QQQ"), regimes)

    _, up = latest_basket(AS_OF, baskets)
    _, down = latest_basket(AS_OF, baskets, scan_name=DOWN_SCAN)
    _, label = latest_regime(AS_OF, regimes)

    assert up.tickers == ["NVDA"] and down.tickers == ["QQQ"]
    assert label.label == UP and label.symbol == "SPY"


@pytest.mark.unit
def test_latest_is_by_created_at_inside_the_file_not_the_file_name(dirs):
    baskets, _, _ = dirs
    newer = save_basket(_basket(("NVDA",), created_at=T0 + timedelta(hours=1)), baskets)
    older = save_basket(_basket(("SPY",), created_at=T0), baskets)
    # Swap names so the older basket sorts last by file name.
    tmp = newer.with_suffix(".tmp")
    newer.rename(tmp)
    older.rename(newer)
    tmp.rename(older)

    _, basket = latest_basket(AS_OF, baskets)

    assert basket.tickers == ["NVDA"]


@pytest.mark.unit
def test_baskets_from_other_scans_are_ignored(dirs):
    baskets, regimes, _ = dirs
    save_basket(_basket(scan_name="SCAN-Something Else"), baskets)
    with pytest.raises(MissingInput, match="no SCAN-Upward Trend Momentum basket"):
        latest_basket(AS_OF, baskets)
    save_label(_label(SIDE), regimes)
    assert route_for_date(AS_OF, baskets, regimes).blocked == []


@pytest.mark.unit
def test_a_missing_regime_label_stops_the_route(dirs):
    baskets, regimes, _ = dirs
    save_basket(_basket(), baskets)
    save_label(_label(UP, symbol="QQQ"), regimes)
    with pytest.raises(MissingInput, match="no SPY regime label for 2026-09-30"):
        route_for_date(AS_OF, baskets, regimes)


@pytest.mark.unit
@pytest.mark.parametrize("label, present", [(UP, _down_basket), (DOWN, _basket)])
def test_route_for_date_stops_without_the_allowed_side_basket(dirs, label, present):
    baskets, regimes, _ = dirs
    save_basket(present(), baskets)
    save_label(_label(label), regimes)
    with pytest.raises(MissingInput, match="basket for 2026-09-30; it is required"):
        route_for_date(AS_OF, baskets, regimes)


@pytest.mark.unit
def test_route_for_date_reads_both_baskets(dirs):
    baskets, regimes, _ = dirs
    up_path = save_basket(_basket(), baskets)
    down_path = save_basket(_down_basket(), baskets)
    save_label(_label(SIDE), regimes)

    decision = route_for_date(AS_OF, baskets, regimes)

    assert decision.blocked == ["QQQ", "SPY", "NVDA"]
    assert decision.basket_file == str(up_path) and decision.down_basket_file == str(down_path)


@pytest.mark.unit
def test_an_unreadable_file_for_the_date_stops_the_route(dirs):
    """Skipping a broken file could quietly route on an older one."""
    baskets, regimes, _ = dirs
    save_basket(_basket(), baskets)
    save_label(_label(UP), regimes)
    (regimes / "regime_SPY_2026-09-30_20260930T230000Z.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(MissingInput, match="cannot read regime label file"):
        route_for_date(AS_OF, baskets, regimes)


@pytest.mark.unit
def test_an_unreadable_basket_stops_the_route_even_under_side(dirs):
    baskets, regimes, _ = dirs
    save_label(_label(SIDE), regimes)
    (baskets / "scan-breakdown-short-candidates_2026-09-30_20260930T230000Z.json").write_text("{", encoding="utf-8")
    with pytest.raises(MissingInput, match="cannot read basket file"):
        route_for_date(AS_OF, baskets, regimes)


@pytest.mark.unit
def test_decision_round_trips_through_json(dirs):
    _, _, routes = dirs
    decision = _route(SIDE)

    path = save_decision(decision, routes)

    assert path.name == "route_ALL_2026-09-30_20260930T210000Z.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["regime_label"] == "SIDE" and data["up_allowed"] is False and data["down_allowed"] is False
    assert data["blocked"] == ["QQQ", "SPY", "NVDA"]
    assert data["basket_file"] == "up.json" and data["down_basket_file"] == "down.json"
    assert load_decision(path) == decision
    with pytest.raises(FileExistsError):
        save_decision(decision, routes)


@pytest.mark.unit
def test_a_route_written_before_the_extension_still_loads(tmp_path):
    old = {
        "as_of": "2026-09-30", "created_at": T0.isoformat(), "series": "UP", "regime_symbol": "SPY",
        "regime_label": "SIDE", "up_allowed": False, "tradeable": [], "blocked": ["NVDA"],
        "reason": "SPY regime is SIDE", "basket_file": "b.json", "regime_file": "r.json", "schema_version": 1,
    }
    path = tmp_path / "route_UP_2026-09-30_20260930T210000Z.json"
    path.write_text(json.dumps(old), encoding="utf-8")

    decision = load_decision(path)

    assert decision.blocked == ["NVDA"] and decision.down_allowed is False
    assert decision.down_passed == [] and decision.down_basket_file is None


def _plans_written(cli_module, routes, out_dir, symbol):
    argv = ["--as-of", "2026-09-30", "--symbol", symbol, "--route-dir", str(routes), "--out-dir", str(out_dir)]
    return cli_module.main(argv) == 0


@pytest.mark.unit
@pytest.mark.parametrize("label, allowed", [
    (UP, {"UPBot": {"QQQ", "SPY"}}),
    (DOWN, {"DOWNBot": {"NVDA"}}),
    (SIDE, {"SIDEBot": {"QQQ", "SPY", "NVDA"}}),
])
def test_the_playbooks_plan_only_what_the_route_allows(label, allowed, tmp_path):
    """End to end: a route from this router lets each bot plan exactly the names the regime allows."""
    routes = tmp_path / "routes"
    save_decision(_route(label), routes)
    bots = {"UPBot": run_up_bot, "DOWNBot": run_down_bot, "SIDEBot": run_side_bot}

    planned = {}
    for name, bot_cli in bots.items():
        for symbol in ("QQQ", "SPY", "NVDA", "AAPL"):
            if _plans_written(bot_cli, routes, tmp_path / name / symbol, symbol):
                planned.setdefault(name, set()).add(symbol)

    assert planned == allowed


def _argv(dirs, *extra):
    baskets, regimes, routes = dirs
    return ["--as-of", "2026-09-30", "--basket-dir", str(baskets), "--regime-dir", str(regimes),
            "--out-dir", str(routes), *extra]


@pytest.mark.unit
def test_cli_prints_both_bots_blocked_and_the_sidebot_names_under_side(dirs, capsys):
    baskets, regimes, routes = dirs
    save_basket(_basket(), baskets)
    save_basket(_down_basket(()), baskets)
    save_label(_label(SIDE), regimes)

    assert cli.main(_argv(dirs)) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "SPY regime as of 2026-09-30: SIDE" in out
    assert "UPBot: BLOCKED  (upward scan passed: QQQ, SPY)" in out
    assert "DOWNBot: BLOCKED  (breakdown scan passed: none)" in out
    assert "routed to SIDEBot only: QQQ, SPY" in out
    (path,) = routes.glob("*.json")
    assert load_decision(path).blocked == ["QQQ", "SPY"]


@pytest.mark.unit
def test_cli_prints_upbot_allowed_under_up(dirs, capsys):
    baskets, regimes, _ = dirs
    save_basket(_basket(), baskets)
    save_label(_label(UP), regimes)

    assert cli.main(_argv(dirs)) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "UPBot: ALLOWED  (upward scan passed: QQQ, SPY)" in out
    assert "DOWNBot: BLOCKED  (breakdown scan passed: no basket)" in out
    assert "SIDEBot" not in out


@pytest.mark.unit
def test_cli_prints_downbot_allowed_under_down(dirs, capsys):
    baskets, regimes, _ = dirs
    save_basket(_down_basket(), baskets)
    save_label(_label(DOWN), regimes)

    assert cli.main(_argv(dirs)) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "UPBot: BLOCKED  (upward scan passed: no basket)" in out
    assert "DOWNBot: ALLOWED  (breakdown scan passed: NVDA)" in out


@pytest.mark.unit
@pytest.mark.parametrize("label, present", [(UP, _down_basket), (DOWN, _basket)])
def test_cli_writes_no_decision_without_the_allowed_side_basket(dirs, capsys, label, present):
    baskets, regimes, routes = dirs
    save_basket(present(), baskets)
    save_label(_label(label), regimes)

    assert cli.main(_argv(dirs)) == cli.EXIT_FAILED

    assert "no decision written" in capsys.readouterr().err
    assert not routes.exists() or list(routes.iterdir()) == []


@pytest.mark.unit
def test_cli_writes_no_decision_without_a_label(dirs, capsys):
    baskets, _, routes = dirs
    save_basket(_basket(), baskets)

    assert cli.main(_argv(dirs)) == cli.EXIT_FAILED

    assert "no decision written" in capsys.readouterr().err
    assert not routes.exists() or list(routes.iterdir()) == []


@pytest.mark.unit
def test_cli_uses_the_default_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".tradingagents"
    save_basket(_basket(), root / "baskets")
    save_label(_label(UP), root / "regimes")

    assert cli.main(["--as-of", "2026-09-30"]) == cli.EXIT_OK

    assert len(list((root / "routes").glob("route_ALL_*.json"))) == 1


@pytest.mark.unit
def test_cli_refuses_a_bad_date(dirs):
    assert cli.main(["--as-of", "30/09/2026"]) == cli.EXIT_BAD_INPUT


@pytest.mark.unit
def test_route_decision_still_accepts_the_original_fields():
    """Callers that build a decision with only the original fields keep working."""
    decision = RouteDecision(
        as_of=AS_OF, created_at=T0, series="UP", regime_symbol="SPY", regime_label="SIDE",
        up_allowed=False, tradeable=[], blocked=["NVDA"], reason="r", basket_file="b.json", regime_file="r.json",
    )
    assert decision.down_allowed is False and decision.up_passed == []
