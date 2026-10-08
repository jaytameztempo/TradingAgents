"""Channel rank scores the saved SIDE baskets with the SCANBot.md 7 weights, caps long names at 4 per
sector and 15 names, keeps and ranks every short name, scores missing inputs 0, and never overwrites.
No network.
"""

import json
from datetime import UTC, date, datetime

import pytest

from extensions.scanbot import channel_rank as cr
from extensions.scanbot.sector_map import GICS_SECTOR, SECTOR_SOURCE, UNKNOWN_SECTOR
from extensions.scripts import run_scanbot_channel_rank as cli

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
FIN_PATH = "fin.json"


def _touches(n):
    return [{"date": "2026-09-01", "price": 1.0, "confirmed_on": "2026-09-02"}] * n


def long_call(dist=0.0, adx=10.0, atr=4.0, touches=6, quality=4):
    return {
        "passed": True,
        "quality_passed": quality,
        "metrics": {
            "distance_to_support_atr": dist,
            "adx_14": adx,
            "atr_pct": atr,
            "support_touches": _touches(touches),
            "resistance_touches": _touches(touches),
        },
    }


def short_call(dist=0.0, adx=10.0, touches=6, quality=4, borrow=True):
    return {
        "passed": True,
        "quality_passed": quality,
        "metrics": {
            "distance_to_resistance_atr": dist,
            "adx_14": adx,
            "atr_pct": 5.0,
            "support_touches": _touches(touches),
            "resistance_touches": _touches(touches),
            "shortable": borrow,
            "easy_to_borrow": borrow,
        },
    }


STRONG_FULL = {"free_cash_flow": 1e9, "interest_coverage": 5.0, "current_ratio": 2.0, "quality_checks": {}}
WEAK_FULL = {
    "quality_checks": {
        "free_cash_flow_negative": True,
        "current_ratio_below_1_0": True,
        "interest_coverage_below_1_or_negative_operating_income": True,
        "going_concern_or_falling_gross_margin_or_dilution": True,
        "operating_margin_below_sector_median": True,
    }
}
LIQ_FULL = {"avg_dollar_volume_20": 5e8, "median_spread_pct": 0.05}


def reports(long_calls, short_calls, strong=None, weak=None, liq=None, as_of=AS_OF):
    long_report = {
        "scan_name": cr.LONG_SCAN,
        "as_of": as_of.isoformat(),
        "financials_report": FIN_PATH,
        "members": list(long_calls),
        "calls": long_calls,
        "warnings": ["long warning"],
    }
    short_report = {
        "scan_name": cr.SHORT_SCAN,
        "as_of": AS_OF.isoformat(),
        "financials_report": FIN_PATH,
        "members": list(short_calls),
        "calls": short_calls,
        "warnings": [],
    }
    financials = {
        "as_of": AS_OF.isoformat(),
        "universe_report": "uni.json",
        "passes": [
            {"financial_gate": "STRONG", "survivor_metrics": strong or {}},
            {"financial_gate": "WEAK", "survivor_metrics": weak or {}},
        ],
    }
    universe = {"survivor_metrics": liq or {}}
    return long_report, short_report, financials, universe


# --- weights and terms ---------------------------------------------------------


def test_weights_match_section_7():
    assert cr.LONG_WEIGHTS == {
        "range_cleanliness": 35, "pivot_proximity": 30, "volatility_fit": 15, "liquidity": 10, "financials": 10,
    }
    assert cr.SHORT_WEIGHTS == {
        "range_cleanliness": 30, "pivot_proximity": 30, "financials": 20, "liquidity": 15, "relative_weakness": 5,
    }
    assert sum(cr.STRONG_POINTS.values()) == 10
    assert sum(cr.WEAK_POINTS.values()) == 20
    assert cr.DOLLAR_VOLUME_POINTS + cr.SPREAD_POINTS == 10
    assert cr.SECTOR_CAP == 4 and cr.MAX_NAMES == 15


def test_perfect_long_and_short_reach_their_maximums():
    long_parts = cr.score_long(long_call(), STRONG_FULL, LIQ_FULL)
    assert sum(long_parts.values()) == pytest.approx(100.0)
    short_parts = cr.score_short(short_call(), WEAK_FULL, LIQ_FULL)
    assert sum(short_parts.values()) == pytest.approx(95.0)  # relative weakness has no input
    assert short_parts["relative_weakness"] == 0


@pytest.mark.parametrize(
    "dist, expected",
    [(0.0, 30.0), (0.3, 15.0), (0.6, 0.0), (0.9, 0.0), (-0.125, 15.0), (-0.25, 0.0)],
)
def test_long_pivot_proximity_scales_to_scan_limits(dist, expected):
    assert cr.pivot_proximity(dist, 30.0, through_is_positive=False) == pytest.approx(expected)


@pytest.mark.parametrize("dist, expected", [(0.0, 30.0), (-0.3, 15.0), (-0.6, 0.0), (0.125, 15.0), (0.25, 0.0)])
def test_short_pivot_proximity_is_mirrored(dist, expected):
    assert cr.pivot_proximity(dist, 30.0, through_is_positive=True) == pytest.approx(expected)


@pytest.mark.parametrize("atr, expected", [(2.0, 0.0), (2.5, 7.5), (3.0, 15.0), (5.0, 15.0), (6.5, 7.5), (8.0, 0.0), (9.0, 0.0)])
def test_volatility_fit(atr, expected):
    assert cr.volatility_fit(atr, 15.0) == pytest.approx(expected)


def test_liquidity_scales():
    assert cr.liquidity_points(2e7, 0.15) == pytest.approx(0.0)
    assert cr.liquidity_points(5e8, 0.05) == pytest.approx(10.0)
    assert cr.liquidity_points(5e9, 0.01) == pytest.approx(10.0)
    assert cr.liquidity_points(1e7, 0.20) == pytest.approx(0.0)


def test_range_cleanliness_parts():
    assert cr.range_cleanliness(long_call(touches=6, adx=10, quality=4), 35) == pytest.approx(35)
    assert cr.range_cleanliness(long_call(touches=1, adx=20, quality=0), 35) == pytest.approx(0)
    # the thinner side sets the touch count
    call = long_call(touches=6, adx=20, quality=0)
    call["metrics"]["resistance_touches"] = _touches(2)
    assert cr.range_cleanliness(call, 35) == pytest.approx(35 * 3 / 7 * 1 / 5)


def test_strong_financials_ignore_margin_and_null_checks():
    assert cr.strong_financials_points(STRONG_FULL) == 10
    fin = {"free_cash_flow": None, "interest_coverage": None, "current_ratio": None,
           "quality_checks": {"operating_margin_above_sector_median": True}}
    assert cr.strong_financials_points(fin) == 0
    assert cr.strong_financials_points({"free_cash_flow": -5, "interest_coverage": 3.0, "current_ratio": 1.2}) == 3


def test_weak_financials_count_only_true_checks():
    checks = dict(WEAK_FULL["quality_checks"], operating_margin_below_sector_median=None, current_ratio_below_1_0=False)
    assert cr.weak_financials_points({"quality_checks": checks}) == 13


def test_missing_inputs_score_zero():
    empty = {"passed": True}
    assert sum(cr.score_long(empty, None, None).values()) == 0
    assert sum(cr.score_short(empty, None, None).values()) == 0
    assert cr.borrow_points({"shortable": True, "easy_to_borrow": None}) == 0


# --- caps --------------------------------------------------------------------


def _row(sym, sector, score):
    return {"symbol": sym, "sector": sector, "score": score}


def test_sector_cap_and_max_names():
    rows = [_row(f"H{i}", "Health Care", 90 - i) for i in range(6)]
    rows += [_row(f"X{i:02d}", f"S{i % 6}", 50 - i) for i in range(14)]
    out = cr.apply_caps(rows)
    status = {r["symbol"]: r["status"] for r in out}
    emitted = [s for s, st in status.items() if st == cr.EMITTED]
    assert emitted == ["H0", "H1", "H2", "H3"] + [f"X{i:02d}" for i in range(11)]
    assert [s for s, st in status.items() if st == cr.DROPPED_SECTOR_CAP] == ["H4", "H5"]
    assert [s for s, st in status.items() if st == cr.DROPPED_MAX_NAMES] == ["X11", "X12", "X13"]
    assert [r["rank"] for r in out] == list(range(1, 21))


def test_cap_skips_capped_name_but_takes_the_next():
    rows = [_row(f"A{i}", "A", 100 - i) for i in range(5)] + [_row("B0", "B", 1)]
    out = {r["symbol"]: r["status"] for r in cr.apply_caps(rows)}
    assert out["A4"] == cr.DROPPED_SECTOR_CAP
    assert out["B0"] == cr.EMITTED


def test_ties_break_by_symbol():
    out = cr.apply_caps([_row("ZZ", "A", 10), _row("AA", "B", 10)])
    assert [r["symbol"] for r in out] == ["AA", "ZZ"]


# --- end to end --------------------------------------------------------------


def test_rank_channels_keeps_every_short_and_ranks_them(monkeypatch):
    monkeypatch.setattr(cr, "sector_of", lambda s: "Health Care")
    long_calls = {f"L{i:02d}": long_call(dist=i * 0.03) for i in range(20)}
    short_calls = {f"S{i}": short_call(dist=-i * 0.1) for i in range(6)}
    rep = cr.rank_channels(*reports(long_calls, short_calls), now=CREATED)
    assert rep.long_emitted == ["L00", "L01", "L02", "L03"]  # one sector, cap 4
    assert rep.short_kept == [f"S{i}" for i in range(6)]
    assert all(r["status"] == cr.KEPT_SHORT for r in rep.short_ranked)
    assert [r["rank"] for r in rep.short_ranked] == list(range(1, 7))


def test_rank_channels_real_names_use_operator_map_and_warn():
    long_calls = {"WMT": long_call(), "NOTMAPPED": long_call()}
    short_calls = {"IONQ": short_call(), "VIR": short_call()}
    rep = cr.rank_channels(
        *reports(long_calls, short_calls, strong={"WMT": STRONG_FULL}, liq={"WMT": LIQ_FULL}), now=CREATED
    )
    sectors = {r["symbol"]: r["sector"] for r in rep.long_ranked + rep.short_ranked}
    assert sectors == {"WMT": "Consumer Staples", "NOTMAPPED": UNKNOWN_SECTOR,
                       "IONQ": "Information Technology", "VIR": "Health Care"}
    assert rep.long_ranked[0]["symbol"] == "WMT" and rep.long_ranked[0]["score"] == pytest.approx(100.0)
    text = "\n".join(rep.warnings)
    assert "NOTMAPPED: not in the operator sector map" in text
    assert "NOTMAPPED: no financials metrics" in text
    assert "IONQ: no universe liquidity metrics" in text
    assert "rs_63" in text and "long warning" in text
    assert rep.policy["sector_source"] == SECTOR_SOURCE == "operator-supplied"


def test_rank_channels_rejects_mismatched_as_of():
    with pytest.raises(ValueError):
        cr.rank_channels(*reports({"A": long_call()}, {}, as_of=date(2026, 9, 29)), now=CREATED)


def test_sector_map_covers_the_24_names():
    names = ("AFRM ALGM AMT CI DG EQPT HAS INCY LIVN LTH MO OPCH ORCL ORLY OTEX PPC TKO UAL VG VST WMT ZM IONQ VIR").split()
    assert sorted(GICS_SECTOR) == sorted(names)


def test_save_round_trip_and_never_overwrite(tmp_path):
    rep = cr.rank_channels(*reports({"WMT": long_call()}, {"VIR": short_call()}), now=CREATED)
    path = cr.save_report(rep, tmp_path)
    assert path.name == "channel_rank_2026-09-30_20261008T120000Z.json"
    loaded = cr.load_report(path)
    assert loaded.long_emitted == ["WMT"] and loaded.short_kept == ["VIR"]
    assert json.loads(path.read_text())["long_emitted"] == ["WMT"]
    with pytest.raises(FileExistsError):
        cr.save_report(rep, tmp_path)


# --- CLI ---------------------------------------------------------------------


def _write_inputs(folder):
    long_report, short_report, financials, universe = reports(
        {"WMT": long_call(), "MO": long_call(dist=0.3)}, {"IONQ": short_call(), "VIR": short_call(dist=-0.3)}
    )
    fin_path, uni_path = folder / "financials_x.json", folder / "universe_x.json"
    long_report["financials_report"] = short_report["financials_report"] = str(fin_path)
    financials["universe_report"] = str(uni_path)
    (folder / "long_channel_2026-09-30_20261001T000000Z.json").write_text(json.dumps({"stale": True}))
    (folder / "long_channel_2026-09-30_20261008T000000Z.json").write_text(json.dumps(long_report))
    (folder / "short_channel_2026-09-30_20261008T000000Z.json").write_text(json.dumps(short_report))
    fin_path.write_text(json.dumps(financials))
    uni_path.write_text(json.dumps(universe))


def test_cli_uses_newest_reports_and_writes(tmp_path, capsys):
    _write_inputs(tmp_path)
    out = tmp_path / "out"
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path), "--out-dir", str(out)]) == cli.EXIT_OK
    written = list(out.glob("channel_rank_2026-09-30_*.json"))
    assert len(written) == 1
    data = json.loads(written[0].read_text())
    assert data["long_emitted"] == ["WMT", "MO"] and data["short_kept"] == ["IONQ", "VIR"]
    assert "SHORT_SIDE (2): IONQ VIR" in capsys.readouterr().out


def test_cli_needs_both_channel_reports(tmp_path):
    _write_inputs(tmp_path)
    for p in tmp_path.glob("short_channel_*.json"):
        p.unlink()
    assert cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path), "--out-dir", str(tmp_path)]) == cli.EXIT_BAD_INPUT
    assert not list(tmp_path.glob("channel_rank_*.json"))


def test_cli_bad_date():
    assert cli.main(["--as-of", "30-09-2026"]) == cli.EXIT_BAD_INPUT
