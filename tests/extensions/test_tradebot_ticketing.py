"""Tradebot ticketing: trend levels, the short borrow check, and the book stop-risk held under 8% across runs."""

import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from extensions.status.confirmation import MarketData
from extensions.status.paper_tickets import Account
from extensions.tradebot import store, ticketing
from extensions.tradebot.ticketing import TicketingError, plan_trend_ticket, risk_book

REPO_ROOT = Path(__file__).resolve().parents[2]
AS_OF = date(2026, 9, 30)
ACCOUNT = Account(100_000.0, 100_000.0, datetime(2026, 9, 30, 21, tzinfo=UTC), "test")


def _bars(symbol, phase=0.0, sign=1, n=160):
    days = pd.bdate_range(end=pd.Timestamp(AS_OF), periods=n)
    i = np.arange(n)
    c = 100 + sign * 0.2 * i + 8 * np.sin(i / 6 + phase)
    return pd.DataFrame({"symbol": symbol, "date": days, "open": c, "high": c + 1, "low": c - 1, "close": c,
                         "volume": 1e6})


def _market(*symbols):
    frames = [_bars("SPY"), _bars("QQQ", phase=1)]
    frames += [_bars(s, sign=-1 if s.startswith("DN") else 1) for s in symbols]
    return MarketData(pd.concat(frames, ignore_index=True), pd.DataFrame(), "test")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(store.HOME_ENV, str(tmp_path))
    return tmp_path


def _triggered(*tickers, bot="UP", side="long", kind="up_cat", stored_swing=119.21):
    items = []
    for t in tickers:
        item = store.new_item(ticker=t, as_of=AS_OF.isoformat(), kind=kind, scan="s",
                              scan_file=f"scan-up-category-1_{AS_OF}_x.json", side=side, needs_regime=bot,
                              metrics={"last_swing_low": stored_swing, "last_swing_high": stored_swing},
                              source="auto")
        items.append(item)
    store.enqueue("trade", items)
    with store.transaction() as q:
        for item in q["trade"]:
            if item["ticker"] in tickers:
                store.set_status(item, "triggered", "test")
    return [i["id"] for i in items]


def _position(symbol="HELD", qty=1000, price=50.0):
    return {"symbol": symbol, "qty": str(qty), "current_price": str(price), "side": "long"}


def _stop(symbol="HELD", stop=44.0):
    return {"symbol": symbol, "type": "limit", "order_class": "oco", "client_order_id": f"x-{symbol}-exit",
            "legs": [{"symbol": symbol, "type": "stop", "stop_price": str(stop)}]}


def _ticket(home, readers=None, ids=None, dry_run=False, market=None):
    r = {"account": lambda: ACCOUNT, "positions": list, "orders": list,
         "asset": lambda s: {"shortable": True, "easy_to_borrow": True}, **(readers or {})}
    return ticketing.ticket_triggered(AS_OF, AS_OF, market or _market("UPA", "UPB", "DNA"), r["account"],
                                      r["positions"], r["orders"], r["asset"], ids, dry_run)


# --- levels ----------------------------------------------------------------------

@pytest.mark.unit
def test_a_long_trend_ticket_enters_at_the_pullback_with_a_2r_target():
    p = plan_trend_ticket("long", close=105.0, sma20=100.0, atr=2.0, swing={"price": 97.0, "date": "2026-09-20"})
    assert (p["entry"], p["stop_calculated"], p["stop"]) == (100.0, 95.0, 94.99)  # 97 - 1 ATR, snapped to .99
    assert p["stop_offset"] == 5.01 and p["target"] == 110.02 and p["reward_per_share"] == 10.02


@pytest.mark.unit
def test_a_short_trend_ticket_enters_at_the_rally_with_a_2r_target():
    p = plan_trend_ticket("short", close=95.0, sma20=100.0, atr=2.0, swing={"price": 103.0})
    assert (p["entry"], p["stop"], p["stop_offset"], p["target"]) == (100.0, 105.01, 5.01, 89.98)


@pytest.mark.unit
def test_a_close_below_the_sma_enters_at_the_close():
    assert plan_trend_ticket("long", close=98.0, sma20=100.0, atr=1.0, swing={"price": 95.0})["entry"] == 98.0


@pytest.mark.unit
@pytest.mark.parametrize("swing", [None, {"price": None}, {"price": 101.0}])
def test_no_usable_swing_makes_no_ticket(swing):
    with pytest.raises(ValueError):
        plan_trend_ticket("long", close=100.0, sma20=100.0, atr=0.5, swing=swing)


@pytest.mark.unit
def test_trend_levels_recompute_the_swing_from_the_bars():
    lv = ticketing.trend_levels(_market("UPA").daily, "UPA", "long", AS_OF)
    assert lv["swing"]["date"] == "2026-09-03" and lv["swing"]["price"] == pytest.approx(119.208, abs=1e-3)
    assert lv["close"] > lv["sma20"]


@pytest.mark.unit
def test_no_bar_on_the_levels_date_is_refused():
    with pytest.raises(ValueError, match="no daily bar on 2026-10-01"):
        ticketing.trend_levels(_market("UPA").daily, "UPA", "long", date(2026, 10, 1))


# --- a run -------------------------------------------------------------------------

@pytest.mark.unit
def test_a_run_tickets_marks_items_and_writes_one_file(home):
    ids = _triggered("UPA", "UPB")
    run = _ticket(home)
    assert [t["symbol"] for t in run.tickets] == ["UPA", "UPB"] and run.file
    t = run.tickets[0]
    assert t["client_order_id"] == f"tb-{ids[0]}-1-UPA-buy" and t["exit_order_id"] == f"tb-{ids[0]}-1-UPA-exit"
    assert t["id_prefix"] == f"tb-{ids[0]}-1" and t["attempt"] == 1
    assert t["entry"] == 128.87 and t["stop"] == 116.99 and t["target"] == 152.63
    assert t["quantity"] == 89  # 23% of $100,000 over two names = $11,500, floor(11,500 / 128.87)
    saved = json.loads((ticketing.tickets_dir() / run.file).read_text(encoding="utf-8"))
    assert saved["kind"] == "tradebot" and saved["paper"] is True and saved["orders_placed"] is False
    items = {i["ticker"]: i for i in store.read()["trade"]}
    assert items["UPA"]["status"] == "ticketed" and items["UPA"]["ticket"]["file"] == run.file


@pytest.mark.unit
def test_one_name_is_held_to_fifteen_percent_of_equity(home):
    _triggered("UPA")
    run = _ticket(home, dry_run=True)
    [t] = run.tickets
    assert run.basket["name_notional_split"] == 23_000.0  # the whole basket would go to one name
    assert run.basket["name_cap"] == 15_000.0 and run.basket["name_notional"] == 15_000.0
    assert t["quantity"] == 116 and t["notional"] <= 15_000.0  # floor(15,000 / 128.87)
    assert any("capped at $15,000.00" in w for w in run.warnings)


@pytest.mark.unit
def test_the_cap_is_on_equity_not_buying_power(home):
    _triggered("UPA")
    margin = Account(50_000.0, 200_000.0, ACCOUNT.read_at, "test")  # 4x buying power
    run = _ticket(home, {"account": lambda: margin}, dry_run=True)
    assert run.basket["name_cap"] == 7_500.0 and run.tickets[0]["notional"] <= 7_500.0


@pytest.mark.unit
def test_a_dry_run_writes_nothing(home):
    _triggered("UPA")
    run = _ticket(home, dry_run=True)
    assert run.tickets and run.file is None and run.dry_run
    assert not ticketing.tickets_dir().exists()
    assert store.read()["trade"][0]["status"] == "triggered"
    assert not ticketing.series_path().exists()  # the series file is not recorded either


@pytest.mark.unit
def test_the_sender_and_the_ticket_agree_on_order_ids(home):
    sender = pytest.importorskip("paper_orders.sender")  # loads alpaca.trading; only the test imports it
    _triggered("UPA")
    [t] = _ticket(home).tickets
    assert sender.client_order_id(AS_OF, t, t["id_prefix"]) == t["client_order_id"]
    assert sender.exit_order_id(AS_OF, t, t["id_prefix"]) == t["exit_order_id"]


@pytest.mark.unit
def test_a_second_attempt_gets_new_order_ids(home):
    [item_id] = _triggered("UPA")
    first = _ticket(home).tickets[0]["client_order_id"]
    with store.transaction() as q:  # the entry expired unfilled; the item triggered again
        store.set_status(store.find(q, item_id), "triggered", "again")
    second = _ticket(home).tickets[0]["client_order_id"]
    assert first == f"tb-{item_id}-1-UPA-buy" and second == f"tb-{item_id}-2-UPA-buy"


@pytest.mark.unit
def test_a_scan_swing_that_differs_is_flagged(home):
    _triggered("UPA", stored_swing=110.0)
    run = _ticket(home, dry_run=True)
    assert any("differs from the scan's 110.00" in w for w in run.warnings)


@pytest.mark.unit
def test_a_short_needs_an_easy_to_borrow_asset(home):
    _triggered("DNA", bot="DOWN", side="short", kind="down_cat")
    run = _ticket(home, {"asset": lambda s: {"shortable": True, "easy_to_borrow": False}}, dry_run=True)
    assert not run.tickets and "easy to borrow" in run.skipped["DNA"]
    run = _ticket(home, {"asset": lambda s: (_ for _ in ()).throw(OSError("down"))}, dry_run=True)
    assert "cannot check the short borrow" in run.skipped["DNA"]
    assert _ticket(home, dry_run=True).tickets[0]["side"] == "short"


@pytest.mark.unit
def test_an_up_item_must_be_long(home):
    _triggered("UPA", side="short")
    assert "must be long" in _ticket(home, dry_run=True).skipped["UPA"]


# --- the book stop-risk ----------------------------------------------------------------

@pytest.mark.unit
def test_an_unprotected_position_blocks_every_ticket(home):
    _triggered("UPA")
    with pytest.raises(TicketingError, match="unprotected position HELD"):
        _ticket(home, {"positions": lambda: [_position()]})
    assert store.read()["trade"][0]["status"] == "triggered"


@pytest.mark.unit
def test_an_open_entry_with_no_ticket_blocks(home):
    _triggered("UPA")
    entry = {"symbol": "ZZZ", "type": "limit", "order_class": "simple", "client_order_id": "someone-else", "qty": "10"}
    with pytest.raises(TicketingError, match="has no ticket"):
        _ticket(home, {"orders": lambda: [entry]})


@pytest.mark.unit
def test_open_risk_counts_positions_open_entries_and_ticketed_items(home):
    _triggered("UPA")
    _ticket(home)  # UPA is now ticketed, not sent
    queue = store.read()["trade"]
    upa = queue[0]["ticket"]
    entry = {"symbol": "UPA", "type": "limit", "order_class": "simple", "client_order_id": upa["client_order_id"],
             "qty": str(upa["quantity"]), "filled_qty": "0"}
    book = risk_book(ACCOUNT, [_position()], [_stop(), entry], [])
    assert book.positions[0]["risk"] == 6000.0  # 1000 x (50 - 44)
    assert book.open_entries[0]["risk"] == upa["risk_dollars"]
    assert not book.problems and book.limit == 8000.0
    assert risk_book(ACCOUNT, [], [], queue).ticketed[0]["risk"] == upa["risk_dollars"]


@pytest.mark.unit
def test_sequential_runs_never_exceed_the_book_limit(home):
    readers = {"positions": lambda: [_position()], "orders": lambda: [_stop()]}  # $6,000 at risk, $2,000 left
    _triggered("UPA", "UPB")
    first = _ticket(home, readers)
    first_risk = sum(t["risk_dollars"] for t in first.tickets)
    assert 0 < first_risk <= 2000.0 and first.basket["scale"] < 1
    _triggered("DNA", bot="DOWN", side="short", kind="down_cat")
    second = _ticket(home, readers)
    second_risk = sum(t["risk_dollars"] for t in second.tickets)
    assert second.risk["ticketed"] and second.risk["open_risk"] == round(6000.0 + first_risk, 2)  # counted run 1
    assert first_risk + second_risk <= 2000.0


@pytest.mark.unit
def test_two_runs_at_once_never_exceed_the_book_limit(home):
    ids_a = _triggered("UPA", "UPB")
    ids_b = _triggered("UPC", "UPD")
    bars = home / "bars.pkl"
    _market("UPA", "UPB", "UPC", "UPD").daily.to_pickle(bars)
    code = textwrap.dedent(f"""
        import sys
        from datetime import UTC, date, datetime
        import pandas as pd
        from extensions.status.confirmation import MarketData
        from extensions.status.paper_tickets import Account
        from extensions.tradebot import ticketing
        ids = sys.argv[1].split(",")
        account = Account(100_000.0, 100_000.0, datetime(2026, 9, 30, 21, tzinfo=UTC), "test")
        market = MarketData(pd.read_pickle({str(bars)!r}), pd.DataFrame(), "test")
        position = {{"symbol": "HELD", "qty": "1000", "current_price": "50", "side": "long"}}
        stop = {{"symbol": "HELD", "type": "limit", "order_class": "oco", "client_order_id": "x-HELD-exit",
                 "legs": [{{"symbol": "HELD", "type": "stop", "stop_price": "45"}}]}}
        run = ticketing.ticket_triggered(date(2026, 9, 30), date(2026, 9, 30), market, lambda: account,
                                         lambda: [position], lambda: [stop], None, ids)
        print(sum(t["risk_dollars"] for t in run.tickets))
    """)
    env = {**os.environ, store.HOME_ENV: str(home)}
    procs = [subprocess.Popen([sys.executable, "-c", code, ",".join(ids)], cwd=REPO_ROOT, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for ids in (ids_a, ids_b)]
    outs = [p.communicate(timeout=180) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1] for o in outs]
    spent = sum(float(o[0].strip()) for o in outs)
    assert 0 < spent <= 3000.0  # $5,000 already at risk of the $8,000 limit; together they never pass it
    ticketed = [i for i in store.read()["trade"] if i["status"] == "ticketed"]
    assert round(sum(i["ticket"]["risk_dollars"] for i in ticketed), 2) == round(spent, 2)


@pytest.mark.unit
def test_ticketing_never_loads_the_trading_client():
    out = subprocess.run([sys.executable, "-c",
                          "import sys, extensions.tradebot.ticketing, extensions.tradebot.triggers, "
                          "extensions.tradebot.enqueue, extensions.scripts.run_enqueue; "
                          "print(sorted(m for m in sys.modules if m.startswith('alpaca')))"],
                         cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
