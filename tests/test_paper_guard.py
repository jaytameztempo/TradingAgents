"""The risk guard: each check in order, settings that fail closed, the peak equity, and the kill switch write."""

import json
import sys

import pytest

from paper_orders import guard, risk_state
from paper_orders.guard import DEFAULT_RISK, filter_entries, validate_risk
from paper_orders.trade_window import SettingsError

ACCOUNT = {"equity": "100000", "last_equity": "100000", "buying_power": "200000"}
RISK = validate_risk({})
EMPTY_STATE = {"state_version": 1, "peak_equity": None, "peak_at": None}


def _t(symbol, qty=100, entry=50.0, stop_offset=2.0):
    return {"symbol": symbol, "quantity": qty, "entry": entry, "stop_offset": stop_offset,
            "risk_dollars": qty * stop_offset}


def _position(symbol, qty=100, price=50.0, value=None):
    return {"symbol": symbol, "qty": str(qty), "current_price": str(price),
            "market_value": str(value if value is not None else qty * price)}


def _stop(symbol, stop):
    return {"symbol": symbol, "type": "limit", "order_class": "oco", "client_order_id": f"x-{symbol}-exit",
            "status": "new", "legs": [{"symbol": symbol, "type": "stop", "stop_price": str(stop)}]}


def _run(tickets, account=ACCOUNT, positions=(), orders=(), risk=RISK, state=EMPTY_STATE):
    return filter_entries(list(tickets), dict(account), list(positions), list(orders), risk, state, [])


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_UI_HOME", str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    return tmp_path


# --- settings ------------------------------------------------------------------------

@pytest.mark.unit
def test_the_defaults_are_the_moderate_limits():
    assert validate_risk(None) == {"kill_switch": False, "daily_loss_pct": 3.0, "drawdown_pct": 10.0,
                                   "max_positions": 15, "max_gross_exposure_pct": 60.0}
    assert DEFAULT_RISK["max_positions"] == 15


@pytest.mark.unit
@pytest.mark.parametrize("bad", [{"daily_loss_pct": 0}, {"daily_loss_pct": "3"}, {"max_positions": 2.5},
                                 {"kill_switch": "yes"}, {"max_gross_exposure_pct": True}, {"typo_pct": 3}, []])
def test_a_wrong_value_is_refused_not_defaulted(bad):
    with pytest.raises(SettingsError):
        validate_risk(bad)


@pytest.mark.unit
def test_set_kill_switch_keeps_every_other_setting(home):
    (home / "settings.json").write_text(json.dumps({"trading": {"days": ["mon"]}, "risk": {"max_positions": 5}}),
                                        encoding="utf-8")
    section = guard.set_kill_switch(True, "test")
    saved = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert saved["trading"] == {"days": ["mon"]} and saved["risk"]["max_positions"] == 5
    assert section["kill_switch"] is True and saved["risk"]["kill_switch_reason"] == "test"
    assert guard.load_risk()["kill_switch"] is True


# --- checks, in order ---------------------------------------------------------------------

@pytest.mark.unit
def test_a_clean_account_allows_every_ticket():
    d, state = _run([_t("A"), _t("B")])
    assert [t["symbol"] for t in d.allowed] == ["A", "B"] and not d.skipped and d.blocked is None
    assert state["peak_equity"] == 100000.0


@pytest.mark.unit
def test_the_kill_switch_blocks_everything():
    d, _ = _run([_t("A")], risk={**RISK, "kill_switch": True, "kill_switch_reason": "manual"})
    assert d.blocked.startswith("kill switch is on (manual)") and d.skipped["A"] == d.blocked and not d.allowed


@pytest.mark.unit
@pytest.mark.parametrize("account", [{}, {"equity": "100000"}, {"equity": "0", "last_equity": "1"},
                                     {"equity": "x", "last_equity": "100000"}])
def test_no_usable_equity_blocks(account):
    assert "no usable equity" in _run([_t("A")], account=account)[0].blocked


@pytest.mark.unit
def test_a_daily_loss_at_the_limit_blocks():
    d, _ = _run([_t("A")], account={**ACCOUNT, "equity": "97000"})  # -3.00%
    assert d.blocked.startswith("daily loss 3.00% >= 3%")
    d, _ = _run([_t("A")], account={**ACCOUNT, "equity": "97100"})  # -2.90%
    assert d.blocked is None


@pytest.mark.unit
def test_a_drawdown_from_the_peak_blocks():
    state = {**EMPTY_STATE, "peak_equity": 112000.0}
    d, kept = _run([_t("A")], state=state)  # 100,000 is 10.71% under 112,000
    assert d.blocked.startswith("drawdown 10.71% >= 10%") and kept["peak_equity"] == 112000.0


@pytest.mark.unit
def test_a_new_high_raises_the_peak():
    _, state = _run([_t("A")], account={**ACCOUNT, "equity": "101000"}, state={**EMPTY_STATE, "peak_equity": 100500.0})
    assert state["peak_equity"] == 101000.0


@pytest.mark.unit
def test_a_held_symbol_or_one_with_an_open_order_is_skipped():
    d, _ = _run([_t("HELD"), _t("OPEN"), _t("NEW")], positions=[_position("HELD")],
                orders=[_stop("HELD", 45.0), {"symbol": "OPEN", "type": "limit", "order_class": "simple",
                                              "client_order_id": "x-OPEN-exit", "status": "new"}])
    assert [t["symbol"] for t in d.allowed] == ["NEW"]
    assert "already has an open position or open order" in d.skipped["HELD"] and "OPEN" in d.skipped


@pytest.mark.unit
def test_the_position_limit_counts_positions_and_the_batch():
    positions = [_position(f"P{i}", qty=1, price=10.0) for i in range(14)]
    orders = [_stop(f"P{i}", 9.0) for i in range(14)]
    d, _ = _run([_t("A", qty=1), _t("B", qty=1)], positions=positions, orders=orders)
    assert [t["symbol"] for t in d.allowed] == ["A"] and "the limit is 15" in d.skipped["B"]


@pytest.mark.unit
def test_gross_exposure_is_held_to_sixty_percent_of_equity():
    # 50,000 already in positions; a 6,000 and a 5,000 ticket: the first fits (56,000), the second would make 61,000
    d, _ = _run([_t("A", qty=120), _t("B", qty=100)], positions=[_position("HELD", qty=1000, price=50.0)],
                orders=[_stop("HELD", 49.0)])
    assert [t["symbol"] for t in d.allowed] == ["A"] and "60% of equity (60,000.00)" in d.skipped["B"]


@pytest.mark.unit
def test_the_book_stop_risk_is_re_checked_on_the_live_account():
    # 7,000 at risk on the position (1,000 x 7); 1,000 left of the 8,000 limit
    d, _ = _run([_t("A", qty=600, entry=10.0, stop_offset=2.0), _t("B", qty=100, entry=10.0, stop_offset=2.0)],
                positions=[_position("HELD", qty=1000, price=50.0, value=5000)], orders=[_stop("HELD", 43.0)])
    assert [t["symbol"] for t in d.allowed] == ["B"] and "over the book's room 1,000.00" in d.skipped["A"]


@pytest.mark.unit
def test_an_unprotected_position_blocks_every_entry():
    d, _ = _run([_t("A")], positions=[_position("NAKED")])
    assert "unprotected position NAKED" in d.blocked


@pytest.mark.unit
def test_describe_prints_the_numbers_and_each_decision():
    d, _ = _run([_t("A"), _t("B", qty=10_000)])
    text = "\n".join(guard.describe(d))
    assert "guard: equity 100,000.00" in text and "A     allowed" in text and "B     SKIPPED" in text


# --- the peak file --------------------------------------------------------------------------

@pytest.mark.unit
def test_the_peak_survives_a_restart_and_a_broken_file_is_an_error(home):
    risk_state.save(risk_state.record_equity(risk_state.load(), 123.0))
    assert risk_state.load()["peak_equity"] == 123.0
    risk_state.state_path().write_text("{broken", encoding="utf-8")
    with pytest.raises(risk_state.RiskStateError, match="reset the peak"):
        risk_state.load()


@pytest.mark.unit
def test_the_guard_loads_no_alpaca_module():
    import subprocess

    out = subprocess.run([sys.executable, "-c", "import sys, paper_orders.guard, paper_orders.risk_state; "
                          "print(sorted(m for m in sys.modules if m.startswith('alpaca')))"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
