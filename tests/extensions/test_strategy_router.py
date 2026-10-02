"""The router allows UP playbooks only under an UP regime, and makes no decision without both inputs.

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
    latest_basket,
    latest_regime,
    load_decision,
    route,
    route_for_date,
    save_decision,
)
from extensions.scans import upward_trend_momentum
from extensions.scans.basket import BasketMember, Rejection, TickerBasket, save_basket
from extensions.scripts import run_router as cli

AS_OF = date(2026, 9, 30)
T0 = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_fetching(monkeypatch):
    """The router reads files only; any bar fetch is a bug."""
    for module in (upward_trend_momentum, regime_bot):
        monkeypatch.setattr(module, "fetch_daily_bars", lambda *a, **k: pytest.fail("fetched bars"))


def _basket(tickers=("QQQ", "SPY"), as_of=AS_OF, created_at=T0, scan_name=upward_trend_momentum.SCAN_NAME):
    return TickerBasket(
        scan_name=scan_name,
        as_of=as_of,
        created_at=created_at,
        universe=[*tickers, "AAPL"],
        parameters={},
        members=[BasketMember(t, 0.01, {}) for t in tickers],
        rejected=[Rejection("AAPL", "no bars")],
    )


def _label(label=UP, symbol="SPY", as_of=AS_OF, created_at=T0):
    return RegimeLabel(
        symbol=symbol, as_of=as_of, created_at=created_at, label=label, reasons=[], metrics={}, parameters={}
    )


@pytest.fixture
def dirs(tmp_path):
    baskets, regimes, routes = tmp_path / "baskets", tmp_path / "regimes", tmp_path / "routes"
    baskets.mkdir()
    regimes.mkdir()
    return baskets, regimes, routes


@pytest.mark.unit
def test_up_regime_allows_up_playbooks_for_the_passed_tickers():
    decision = route(_basket(), _label(UP), "b.json", "r.json", created_at=T0)
    assert decision.up_allowed
    assert decision.tradeable == ["QQQ", "SPY"]
    assert decision.blocked == []
    assert "allowed" in decision.reason


@pytest.mark.unit
@pytest.mark.parametrize("label", [SIDE, DOWN])
def test_side_and_down_block_up_playbooks(label):
    decision = route(_basket(), _label(label), "b.json", "r.json", created_at=T0)
    assert not decision.up_allowed
    assert decision.tradeable == []
    assert decision.blocked == ["QQQ", "SPY"]
    assert f"regime is {label}: UP playbooks are blocked" in decision.reason


@pytest.mark.unit
def test_rejected_tickers_are_neither_tradeable_nor_blocked():
    decision = route(_basket(), _label(UP), "b.json", "r.json")
    assert "AAPL" not in decision.tradeable + decision.blocked


@pytest.mark.unit
def test_a_basket_and_label_for_different_dates_are_refused():
    with pytest.raises(ValueError, match="regime label is for"):
        route(_basket(), _label(UP, as_of=AS_OF - timedelta(days=1)), "b.json", "r.json")


@pytest.mark.unit
def test_the_newest_basket_and_label_for_the_date_win(dirs):
    baskets, regimes, _ = dirs
    save_basket(_basket(("SPY",), created_at=T0), baskets)
    save_basket(_basket(("NVDA",), created_at=T0 + timedelta(hours=1)), baskets)
    save_basket(_basket(("MSFT",), created_at=T0 + timedelta(hours=2), as_of=AS_OF - timedelta(days=1)), baskets)
    save_label(_label(SIDE, created_at=T0), regimes)
    save_label(_label(UP, created_at=T0 + timedelta(hours=1)), regimes)
    save_label(_label(DOWN, created_at=T0 + timedelta(hours=2), symbol="QQQ"), regimes)

    _, basket = latest_basket(AS_OF, baskets)
    _, label = latest_regime(AS_OF, regimes)

    assert basket.tickers == ["NVDA"]
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
    baskets, _, _ = dirs
    save_basket(_basket(scan_name="SCAN-Something Else"), baskets)
    with pytest.raises(MissingInput, match="no SCAN-Upward Trend Momentum basket"):
        latest_basket(AS_OF, baskets)


@pytest.mark.unit
def test_a_missing_regime_label_stops_the_route(dirs):
    baskets, regimes, _ = dirs
    save_basket(_basket(), baskets)
    save_label(_label(UP, symbol="QQQ"), regimes)
    with pytest.raises(MissingInput, match="no SPY regime label for 2026-09-30"):
        route_for_date(AS_OF, baskets, regimes)


@pytest.mark.unit
def test_a_missing_basket_stops_the_route(dirs):
    baskets, regimes, _ = dirs
    save_label(_label(UP), regimes)
    with pytest.raises(MissingInput, match="basket for 2026-09-30"):
        route_for_date(AS_OF, baskets, regimes)


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
def test_decision_round_trips_through_json(dirs):
    _, _, routes = dirs
    decision = route(_basket(), _label(SIDE), "b.json", "r.json", created_at=T0)

    path = save_decision(decision, routes)

    assert path.name == "route_UP_2026-09-30_20260930T210000Z.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["regime_label"] == "SIDE" and data["up_allowed"] is False
    assert data["blocked"] == ["QQQ", "SPY"]
    assert load_decision(path) == decision
    with pytest.raises(FileExistsError):
        save_decision(decision, routes)


def _argv(dirs, *extra):
    baskets, regimes, routes = dirs
    return ["--as-of", "2026-09-30", "--basket-dir", str(baskets), "--regime-dir", str(regimes),
            "--out-dir", str(routes), *extra]


@pytest.mark.unit
def test_cli_prints_blocked_tickers_under_side(dirs, capsys):
    baskets, regimes, routes = dirs
    save_basket(_basket(), baskets)
    save_label(_label(SIDE), regimes)

    assert cli.main(_argv(dirs)) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "SPY regime as of 2026-09-30: SIDE" in out
    assert "UP playbooks: BLOCKED" in out
    assert "not tradeable under UP: QQQ, SPY" in out
    (path,) = routes.glob("*.json")
    assert load_decision(path).blocked == ["QQQ", "SPY"]


@pytest.mark.unit
def test_cli_prints_tradeable_tickers_under_up(dirs, capsys):
    baskets, regimes, _ = dirs
    save_basket(_basket(), baskets)
    save_label(_label(UP), regimes)

    assert cli.main(_argv(dirs)) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "UP playbooks: ALLOWED" in out
    assert "tradeable under UP: QQQ, SPY" in out


@pytest.mark.unit
def test_cli_blocks_under_down(dirs, capsys):
    baskets, regimes, _ = dirs
    save_basket(_basket(), baskets)
    save_label(_label(DOWN), regimes)

    assert cli.main(_argv(dirs)) == cli.EXIT_OK
    assert "UP playbooks: BLOCKED" in capsys.readouterr().out


@pytest.mark.unit
def test_cli_writes_no_decision_when_an_input_is_missing(dirs, capsys):
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

    assert len(list((root / "routes").glob("*.json"))) == 1


@pytest.mark.unit
def test_cli_refuses_a_bad_date(dirs):
    assert cli.main(["--as-of", "30/09/2026"]) == cli.EXIT_BAD_INPUT
