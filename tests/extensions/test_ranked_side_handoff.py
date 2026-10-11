"""The ranked SIDE handoff plans the ranker's names in rank order under SIDE only, invents no sector, and never orders.

Inputs are saved channel reports, a channel rank report and routes in tmp folders; Alpaca is made to fail if touched.
"""

import ast
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alpaca.data.historical import StockHistoricalDataClient

from extensions.research import ranked_side_handoff as ranked_mod
from extensions.research.ranked_side_handoff import (
    NOT_RANKED,
    PLAN,
    PLAN_ENDING,
    build_ranked_side_handoff,
    plan_text,
    ranked_side_handoff_for_date,
    save_ranked_side_handoff,
)
from extensions.research.side_handoff import LONG_FADE, SHORT_FADE
from extensions.router.strategy_router import MissingInput, RouteDecision, save_decision
from extensions.scanbot import long_channel, short_channel
from extensions.scanbot.channel_rank import DROPPED_MAX_NAMES, DROPPED_SECTOR_CAP, EMITTED, KEPT_SHORT
from extensions.scanbot.long_channel import LongChannelReport
from extensions.scanbot.short_channel import ShortChannelReport
from extensions.scanbot.side_confirm_4h import latest_ranked_side_handoff, planned_names
from extensions.scripts import run_ranked_side_handoff as cli
from extensions.status.daily_status import build_status, latest_route

AS_OF = date(2026, 9, 30)
T0 = datetime(2026, 10, 8, 2, 0, tzinfo=UTC)
# Long rank order: ORCL, DLB (sector cap), CI, MO (name cap). Short rank order: VIR, IONQ.
LONG_ROWS = [("ORCL", "Information Technology", EMITTED), ("DLB", "Information Technology", DROPPED_SECTOR_CAP),
             ("CI", "Health Care", EMITTED), ("MO", "Consumer Staples", DROPPED_MAX_NAMES)]
SHORT_ROWS = [("VIR", "Health Care", KEPT_SHORT), ("IONQ", "Information Technology", KEPT_SHORT)]
LONGS, SHORTS = [s for s, _, _ in LONG_ROWS], [s for s, _, _ in SHORT_ROWS]


@pytest.fixture(autouse=True)
def _no_alpaca(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("touched Alpaca")

    monkeypatch.setattr(StockHistoricalDataClient, "__init__", refuse)
    monkeypatch.setattr(long_channel, "fetch_daily_bars", refuse)
    monkeypatch.setattr(short_channel, "fetch_daily_bars", refuse)


def _call(symbol, short=False):
    metrics = {"close": 100.0, "support": 90.0, "resistance": 101.0, "height_pct": 11.0, "adx_14": 12.0,
               "atr_14": 2.0, "atr_pct": 2.0, "support_touches": [{}, {}], "resistance_touches": [{}, {}, {}],
               "rsi_14": 50.0, "last_bar_date": "2026-09-30"}
    metrics["distance_to_resistance_atr" if short else "distance_to_support_atr"] = -0.5 if short else 5.0
    return {"passed": True, "reasons": [], "detail": [f"{symbol} channel detail"],
            "quality": {"q1": True, "q2": None}, "quality_passed": 1, "metrics": metrics}


def _common(members, created_at, as_of, short):
    return dict(
        basket_id="b", as_of=as_of, created_at=created_at, financials_report="f.json",
        financials_created_at=T0.isoformat(), policy={}, stage_counts={}, reason_counts={},
        quality_counts={}, members=list(members), calls={s: _call(s, short) for s in members},
        warnings=["not point in time"],
    )


def _long(members=LONGS, as_of=AS_OF):
    return LongChannelReport(scan_name=long_channel.SCAN_NAME, mode=long_channel.MODE, pivot_side="LONG",
                             **_common(members, T0, as_of, False))


def _short(members=SHORTS, as_of=AS_OF):
    return ShortChannelReport(scan_name=short_channel.SCAN_NAME, mode=short_channel.MODE, pivot_side="SHORT",
                              financial_gate="WEAK", assets_source="a.json", assets_fetched_at=T0.isoformat(),
                              **_common(members, T0, as_of, True))


def _rows(rows):
    return [{"symbol": s, "sector": sector, "score": 90.0 - 10 * i, "components": {"range_cleanliness": 20.0},
             "inputs": {}, "rank": i + 1, "status": status} for i, (s, sector, status) in enumerate(rows)]


def _rank(long_rows=LONG_ROWS, short_rows=SHORT_ROWS, created_at=T0, sources=None):
    return {"as_of": AS_OF.isoformat(), "created_at": created_at.isoformat(), "sources": sources or {},
            "long_ranked": _rows(long_rows), "short_ranked": _rows(short_rows),
            "warnings": ["sectors are operator-supplied", "sectors are operator-supplied"]}


def _route(label="SIDE", created_at=T0):
    return RouteDecision(
        as_of=AS_OF, created_at=created_at, series="ALL", regime_symbol="SPY+QQQ", regime_label=label,
        up_allowed=label == "UP", down_allowed=label == "DOWN", tradeable=[], blocked=[],
        reason=f"market is {label}", basket_file="b.json", regime_file="r.json",
        regime_labels={"SPY": label, "QQQ": label},
    )


def _route_dict(label="SIDE"):
    return {"as_of": AS_OF.isoformat(), "regime_symbol": "SPY+QQQ", "regime_label": label}


def _build(label="SIDE", rank=None, longs=LONGS, shorts=SHORTS):
    return build_ranked_side_handoff(rank or _rank(), "rank.json", _long(longs), "l.json", _short(shorts), "s.json",
                                     _route_dict(label), "r.json")


def _save_inputs(tmp_path, label="SIDE", long_rows=LONG_ROWS):
    reports, routes = tmp_path / "scanbot", tmp_path / "routes"
    long_path = long_channel.save_report(_long([s for s, _, _ in long_rows]), reports)
    short_path = short_channel.save_report(_short(), reports)
    rank = _rank(long_rows, sources={"long_channel": str(long_path), "short_channel": str(short_path)})
    (reports / f"channel_rank_{AS_OF}_20261008T020000Z.json").write_text(json.dumps(rank), encoding="utf-8")
    save_decision(_route(label), routes)
    return reports, routes


@pytest.mark.unit
def test_emitted_longs_and_kept_shorts_are_planned_in_rank_order():
    h = _build()
    assert h.planned_long == ["ORCL", "CI"] and h.planned_short == ["VIR", "IONQ"]
    assert h.not_ranked == ["DLB", "MO"]
    assert list(h.names) == ["ORCL", "CI", "VIR", "IONQ", "DLB", "MO"]
    assert {s: h.names[s]["setup"] for s in ("ORCL", "VIR")} == {"ORCL": LONG_FADE, "VIR": SHORT_FADE}
    orcl = h.names["ORCL"]
    assert orcl["status"] == PLAN and orcl["rank"] == 1 and orcl["sector"] == "Information Technology"
    assert orcl["levels"]["invalidation"] == 89.5 and orcl["levels"]["support_touches"] == 2
    assert h.warnings == ["sectors are operator-supplied"]
    assert h.orders_placed is False


@pytest.mark.unit
def test_dropped_longs_are_not_ranked_with_the_ranker_reason():
    h = _build()
    dlb, mo = h.names["DLB"], h.names["MO"]
    assert dlb["status"] == NOT_RANKED and dlb["plan_file"] is None and "levels" not in dlb
    assert dlb["reason"] == ("ranker status dropped_sector_cap: Information Technology already has 4 emitted "
                             "long names (cap 4); no plan written")
    assert mo["reason"].startswith("ranker status dropped_max_names: 15 long names")
    with pytest.raises(ValueError, match="gets no plan"):
        plan_text(h, "DLB")


@pytest.mark.unit
def test_plans_carry_the_rank_and_end_with_no_order():
    h = _build()
    ci, vir = plan_text(h, "CI"), plan_text(h, "VIR")
    assert "- long rank 3 of 4 scored (2 emitted), score 70.0 / 100, sector Health Care" in ci
    assert "- short rank 1 of 2 kept, score 90.0 / 100, sector Health Care" in vir
    assert "a close past 89.5 " in ci and "a close past 101.5 " in vir
    assert ci.rstrip().endswith(PLAN_ENDING) and vir.rstrip().endswith(PLAN_ENDING)


@pytest.mark.unit
@pytest.mark.parametrize("label", ["UP", "DOWN"])
def test_a_trending_market_builds_nothing(label):
    with pytest.raises(ValueError, match=f"market label is {label}, not SIDE"):
        _build(label)


@pytest.mark.unit
def test_a_ranked_name_without_a_mapped_sector_stops_the_build():
    rows = [("ORCL", "Information Technology", EMITTED), ("NEWCO", "Unknown", EMITTED)]
    rank = _rank(rows, [("ZZZ", "Unknown", KEPT_SHORT)])
    with pytest.raises(ValueError, match="no operator-supplied sector: NEWCO, ZZZ"):
        _build(rank=rank, longs=["ORCL", "NEWCO"], shorts=["ZZZ"])


@pytest.mark.unit
def test_rank_and_channel_members_must_agree():
    with pytest.raises(ValueError, match="long channel members"):
        _build(longs=["ORCL", "CI"])


@pytest.mark.unit
def test_save_writes_plans_then_the_handoff_and_never_overwrites(tmp_path):
    h = _build()
    path, plans = save_ranked_side_handoff(h, tmp_path / "h", tmp_path / "p")
    assert path.name.startswith(f"ranked_side_handoff_{AS_OF}_")
    assert [p.name.split("_")[3] for p in plans] == ["ORCL", "CI", "VIR", "IONQ"]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["orders_placed"] is False
    assert all(data["names"][s]["plan_file"] for s in ("ORCL", "CI", "VIR", "IONQ"))
    assert data["names"]["DLB"]["plan_file"] is None
    with pytest.raises(FileExistsError):
        save_ranked_side_handoff(h, tmp_path / "h", tmp_path / "p")


@pytest.mark.unit
def test_for_date_output_is_what_the_later_steps_read(tmp_path):
    reports, routes = _save_inputs(tmp_path)
    h = ranked_side_handoff_for_date(AS_OF, reports, routes)
    path, _ = save_ranked_side_handoff(h, tmp_path / "h", tmp_path / "p")
    found_path, data = latest_ranked_side_handoff(AS_OF, tmp_path / "h")
    assert found_path == path
    assert planned_names(data) == {"long": ["ORCL", "CI"], "short": ["VIR", "IONQ"]}
    status = build_status(AS_OF, latest_route(AS_OF, routes), (path, data), None)
    assert not any("ranked side handoff" in r for r in status.blocked_reasons)


@pytest.mark.unit
def test_newest_rank_report_is_used(tmp_path):
    reports, routes = _save_inputs(tmp_path)
    newer = json.loads(next(reports.glob("channel_rank_*.json")).read_text(encoding="utf-8"))
    newer["created_at"] = (T0 + timedelta(hours=1)).isoformat()
    newer["short_ranked"] = list(reversed(_rows(SHORT_ROWS)))
    for i, row in enumerate(newer["short_ranked"], start=1):
        row["rank"] = i
    (reports / f"channel_rank_{AS_OF}_20261008T030000Z.json").write_text(json.dumps(newer), encoding="utf-8")
    assert ranked_side_handoff_for_date(AS_OF, reports, routes).planned_short == ["IONQ", "VIR"]


@pytest.mark.unit
def test_missing_inputs_stop_the_handoff(tmp_path):
    with pytest.raises(MissingInput, match="no channel rank report"):
        ranked_side_handoff_for_date(AS_OF, tmp_path, tmp_path)


@pytest.mark.unit
def test_cli_writes_handoff_and_plans_under_side(tmp_path, capsys):
    reports, routes = _save_inputs(tmp_path)
    code = cli.main(["--as-of", "2026-09-30", "--report-dir", str(reports), "--route-dir", str(routes),
                     "--out-dir", str(tmp_path / "h"), "--plan-dir", str(tmp_path / "p")])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "planned long (2): ORCL CI" in out and "not ranked: DLB MO" in out
    assert "plans written: 4; no order placed" in out
    assert len(list((tmp_path / "p").glob("ranked_side_research_*.md"))) == 4


@pytest.mark.unit
def test_cli_writes_nothing_when_not_side(tmp_path, capsys):
    reports, routes = _save_inputs(tmp_path, label="UP")
    code = cli.main(["--as-of", "2026-09-30", "--report-dir", str(reports), "--route-dir", str(routes),
                     "--out-dir", str(tmp_path / "h"), "--plan-dir", str(tmp_path / "p")])
    assert code == cli.EXIT_FAILED
    assert "not SIDE" in capsys.readouterr().err
    assert not (tmp_path / "h").exists() and not (tmp_path / "p").exists()


@pytest.mark.unit
def test_cli_rejects_bad_date():
    assert cli.main(["--as-of", "09/30/2026"]) == cli.EXIT_BAD_INPUT


@pytest.mark.unit
def test_ranked_handoff_sources_never_name_alpaca_or_an_order_call():
    for path in (Path(ranked_mod.__file__), Path(cli.__file__)):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "alpaca" not in node.module and "paper_orders" not in node.module, path
            if isinstance(node, ast.Import):
                assert not any("alpaca" in a.name or "paper_orders" in a.name for a in node.names), path
        for word in ("TradingClient", "submit_order", "OrderRequest", "close_position"):
            assert word not in source, (path, word)
