"""SCANBot step 2 runs the strong and weak financial gates as separate passes over the universe
survivors, counts every gate, fails closed on missing or stale data, records the TTM method and
the quality checks without requiring them, and never lets a name pass both. No network.
"""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from extensions.scanbot import financials as fin
from extensions.scanbot import fundamentals as fx
from extensions.scanbot.financials import STRONG, WEAK, load_report, measure, months_before, run_financials, save_report, ttm
from extensions.scanbot.funnel import FunnelReport
from extensions.scanbot.funnel import save_report as save_universe
from extensions.scripts import run_scanbot_financials as cli

AS_OF = date(2026, 9, 30)
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
CREATED = datetime(2026, 10, 6, 12, 5, tzinfo=UTC)

FY = ("2025-01-01", "2025-12-31")
YTD = ("2026-01-01", "2026-06-30")
PRIOR = ("2025-01-01", "2025-06-30")


def _f(period, val, filed, form):
    return {"start": period[0], "end": period[1], "val": val, "filed": filed, "form": form}


def _line(annual, ytd=None, prior=None):
    """Facts for one duration line: FY2025 on a 10-K, 2026 H1 and 2025 H1 on 10-Qs."""
    facts = [_f(FY, annual, "2026-02-15", "10-K")]
    if ytd is not None:
        facts.append(_f(YTD, ytd, "2026-08-05", "10-Q"))
    if prior is not None:
        facts.append(_f(PRIOR, prior, "2025-08-05", "10-Q"))
        facts.append(_f(PRIOR, prior, "2026-08-05", "10-Q"))
    return facts


def _file(symbol, us_gaap, dei=None, taxonomies=("dei", "us-gaap"), status=fx.OK):
    facts = {"us-gaap": {tag: {unit: data} for tag, (unit, data) in us_gaap.items()}}
    if dei:
        facts["dei"] = {tag: {"shares": data} for tag, data in dei.items()}
    return fx.FundamentalsFile(symbol, "0000000001", f"{symbol} Inc", NOW - timedelta(days=1), status,
                               list(taxonomies), facts)


def _company(eps, opinc, ocf, **extra):
    lines = {
        "EarningsPerShareDiluted": ("USD/shares", _line(*eps)),
        "OperatingIncomeLoss": ("USD", _line(*opinc)),
        "NetCashProvidedByUsedInOperatingActivities": ("USD", _line(*ocf)),
    }
    lines.update(extra)
    return lines


STRONG_CO = _company(
    (4.0, 2.5, 2.0), (400e6, 250e6, 200e6), (500e6, 300e6, 250e6),
    PaymentsToAcquirePropertyPlantAndEquipment=("USD", _line(100e6, 60e6, 50e6)),
    InterestExpense=("USD", _line(20e6, 10e6, 10e6)),
    AssetsCurrent=("USD", [{"end": "2026-06-30", "val": 300e6, "filed": "2026-08-05", "form": "10-Q"}]),
    LiabilitiesCurrent=("USD", [{"end": "2026-06-30", "val": 200e6, "filed": "2026-08-05", "form": "10-Q"}]),
)
WEAK_CO = _company(
    (-1.0, -0.6, -0.5), (-50e6, -30e6, -25e6), (-40e6, -25e6, -20e6),
    AssetsCurrent=("USD", [{"end": "2026-06-30", "val": 50e6, "filed": "2026-08-05", "form": "10-Q"}]),
    LiabilitiesCurrent=("USD", [{"end": "2026-06-30", "val": 80e6, "filed": "2026-08-05", "form": "10-Q"}]),
)
WEAK_SHARES = {"EntityCommonStockSharesOutstanding": [
    {"end": "2025-07-31", "val": 100e6, "filed": "2025-08-05", "form": "10-Q"},
    {"end": "2026-07-31", "val": 125e6, "filed": "2026-08-05", "form": "10-Q"},
]}
MIXED_CO = _company((1.0, 0.5, 0.5), (10e6, 5e6, 5e6), (-5e6, -3e6, -2e6))
BANK = {k: v for k, v in STRONG_CO.items() if k != "OperatingIncomeLoss"}
STALE_CO = {
    tag: (unit, [_f(("2024-01-01", "2024-12-31"), val, "2025-02-15", "10-K")])
    for tag, unit, val in (("EarningsPerShareDiluted", "USD/shares", 2.0),
                           ("OperatingIncomeLoss", "USD", 10e6),
                           ("NetCashProvidedByUsedInOperatingActivities", "USD", 10e6))
}

FILES = {
    "GOOD": _file("GOOD", STRONG_CO),
    "BAD": _file("BAD", WEAK_CO, dei=WEAK_SHARES),
    "MIXED": _file("MIXED", MIXED_CO),
    "BANK": _file("BANK", BANK),
    "STALE": _file("STALE", STALE_CO),
    "IFRS": _file("IFRS", {}, taxonomies=("dei", "ifrs-full")),
    "GONE": _file("GONE", {}, taxonomies=(), status=fx.NO_FACTS),
}
SYMBOLS = ["GOOD", "BAD", "MIXED", "BANK", "STALE", "IFRS", "GONE", "NOFILE"]


def _lookups():
    lookups = {s: fx.Lookup(s, ff, fx.CACHE) for s, ff in FILES.items()}
    lookups["NOFILE"] = fx.Lookup("NOFILE", None, failure="company facts fetch failed (HTTP 503); no cached file")
    return lookups


def _universe(survivors=SYMBOLS):
    return FunnelReport(
        as_of=AS_OF, created_at=datetime(2026, 10, 4, 2, 22, tzinfo=UTC), preset="DEFAULT", thresholds={},
        feed="sip", assets_source="assets.json", assets_fetched_at=datetime(2026, 10, 4, 2, 7, tzinfo=UTC),
        point_in_time=False, universe_count=100, sessions=[], spread_method="sampled", spread_sampling={},
        stage_counts=[], survivors=list(survivors), survivor_metrics={}, removed=[],
    )


def _run():
    return run_financials(_universe(), "universe.json", _lookups(), created_at=CREATED)


# --- TTM -----------------------------------------------------------------------------------------

def test_ttm_is_annual_plus_ytd_minus_prior_ytd():
    t = ttm(FILES["GOOD"], fx.OPERATING_INCOME_TAGS, "2026-09-30")
    assert t.value == pytest.approx(400e6 + 250e6 - 200e6)
    assert t.method == "annual+ytd-prior_ytd"
    assert t.period_end == "2026-06-30"
    assert t.periods == {"annual": list(FY), "ytd": list(YTD), "prior_ytd": list(PRIOR)}


def test_ttm_ignores_facts_filed_after_as_of():
    t = ttm(FILES["GOOD"], fx.OPERATING_INCOME_TAGS, "2026-08-01")  # H1 2026 not yet filed
    assert t.method == "annual" and t.value == 400e6 and t.period_end == "2025-12-31"


def test_ttm_uses_annual_when_prior_year_ytd_is_missing():
    ff = _file("X", {"OperatingIncomeLoss": ("USD", _line(400e6, 250e6))})
    t = ttm(ff, fx.OPERATING_INCOME_TAGS, "2026-09-30")
    assert t.method == "annual" and t.value == 400e6


def test_ttm_takes_restatement_only_from_its_filing_date():
    facts = _line(400e6) + [_f(FY, 380e6, "2026-09-01", "10-K/A")]
    ff = _file("X", {"OperatingIncomeLoss": ("USD", facts)})
    assert ttm(ff, fx.OPERATING_INCOME_TAGS, "2026-08-31").value == 400e6
    assert ttm(ff, fx.OPERATING_INCOME_TAGS, "2026-09-30").value == 380e6


def test_ttm_needs_an_annual_filing():
    ff = _file("X", {"OperatingIncomeLoss": ("USD", [_f(YTD, 5e6, "2026-08-05", "10-Q")])})
    assert ttm(ff, fx.OPERATING_INCOME_TAGS, "2026-09-30") is None


def test_months_before():
    assert months_before(date(2026, 9, 30), 15) == date(2025, 6, 30)
    assert months_before(date(2026, 5, 31), 15) == date(2025, 2, 28)


# --- gates ---------------------------------------------------------------------------------------

def _counts(gate_pass):
    return [(c.gate, c.entered, c.removed, c.survived) for c in gate_pass.stage_counts]


def test_strong_pass_counts_every_gate():
    p = _run().gate_pass(STRONG)
    assert _counts(p) == [
        ("fundamentals_data", 8, 3, 5),     # IFRS, GONE, NOFILE
        ("period_recency", 5, 1, 4),        # STALE
        ("diluted_eps", 4, 1, 3),           # BAD
        ("operating_income", 3, 1, 2),      # BANK
        ("operating_cash_flow", 2, 1, 1),   # MIXED
    ]
    assert p.survivors == ["GOOD"]


def test_weak_pass_counts_every_gate():
    p = _run().gate_pass(WEAK)
    assert _counts(p) == [
        ("fundamentals_data", 8, 3, 5),
        ("period_recency", 5, 1, 4),
        ("diluted_eps", 4, 3, 1),           # GOOD, MIXED, BANK
        ("operating_income", 1, 0, 1),
        ("operating_cash_flow", 1, 0, 1),
    ]
    assert p.survivors == ["BAD"]


def test_every_removal_has_a_reason():
    report = _run()
    reasons = {(r.symbol, r.gate): r.reason for r in report.gate_pass(STRONG).removed}
    assert "ifrs-full" in reasons[("IFRS", "fundamentals_data")]
    assert "no company facts" in reasons[("GONE", "fundamentals_data")]
    assert "HTTP 503" in reasons[("NOFILE", "fundamentals_data")]
    assert "2024-12-31" in reasons[("STALE", "period_recency")]
    assert "not reported" in reasons[("BANK", "operating_income")]
    assert "not above 0" in reasons[("MIXED", "operating_cash_flow")]


def test_metric_with_stale_period_fails_closed():
    lines = dict(STRONG_CO)
    lines["OperatingIncomeLoss"] = STALE_CO["OperatingIncomeLoss"]  # the filer stopped using the tag
    lookups = {"OLDTAG": fx.Lookup("OLDTAG", _file("OLDTAG", lines), fx.CACHE)}
    p = run_financials(_universe(["OLDTAG"]), "u.json", lookups, created_at=CREATED).gate_pass(STRONG)
    assert p.survivors == []
    assert p.removed[0].gate == "operating_income" and "more than 15 months" in p.removed[0].reason


def test_zero_passes_neither_gate():
    lookups = {"ZERO": fx.Lookup("ZERO", _file("ZERO", _company((0.0,), (0.0,), (0.0,))), fx.CACHE)}
    report = run_financials(_universe(["ZERO"]), "u.json", lookups, created_at=CREATED)
    assert report.gate_pass(STRONG).survivors == [] and report.gate_pass(WEAK).survivors == []


def test_strong_survivor_records_ttm_method_and_quality_checks():
    m = _run().gate_pass(STRONG).survivor_metrics["GOOD"]
    assert m["ttm"]["operating_cash_flow"]["method"] == "annual+ytd-prior_ytd"
    assert m["ttm"]["diluted_eps"]["value"] == pytest.approx(4.5)
    assert m["free_cash_flow"] == pytest.approx(550e6 - 110e6)
    assert m["current_ratio"] == pytest.approx(1.5)
    assert m["interest_coverage"] == pytest.approx(450e6 / 20e6)
    assert m["quality_checks"] == {
        "free_cash_flow_positive": True,
        "operating_margin_above_sector_median": None,
        "current_ratio_at_least_1_2": True,
        "interest_coverage_above_3": True,
        "no_going_concern_language": None,
    }
    assert (m["quality_passed"], m["quality_available"]) == (3, 3)
    assert m["fundamentals_fetched_at"] == (NOW - timedelta(days=1)).isoformat()


def test_weak_survivor_records_quality_checks():
    m = _run().gate_pass(WEAK).survivor_metrics["BAD"]
    checks = m["quality_checks"]
    assert checks["free_cash_flow_negative"] is None  # no capex tagged
    assert checks["current_ratio_below_1_0"] is True
    assert checks["interest_coverage_below_1_or_negative_operating_income"] is True
    assert checks["detail_shares_up_over_10pct_yoy"] is True
    assert checks["going_concern_or_falling_gross_margin_or_dilution"] is True
    assert m["shares_growth_yoy"] == pytest.approx(0.25)
    assert m["quality_passed"] == 3


def test_falling_gross_margin_over_three_quarters():
    quarters = [("2025-10-01", "2025-12-31"), ("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-30")]
    revenue = [_f(q, 100.0, "2026-08-05", "10-Q") for q in quarters]
    gross = [_f(q, gp, "2026-08-05", "10-Q") for q, gp in zip(quarters, (40.0, 35.0, 30.0))]
    ff = _file("X", {"Revenues": ("USD", revenue), "GrossProfit": ("USD", gross)})
    assert [g[1] for g in measure(ff, AS_OF).extras["gross_margins"]] == [0.40, 0.35, 0.30]


def test_quality_checks_are_not_required():
    lines = {k: v for k, v in STRONG_CO.items()
             if k not in ("PaymentsToAcquirePropertyPlantAndEquipment", "InterestExpense",
                          "AssetsCurrent", "LiabilitiesCurrent")}
    lookups = {"BARE": fx.Lookup("BARE", _file("BARE", lines), fx.CACHE)}
    p = run_financials(_universe(["BARE"]), "u.json", lookups, created_at=CREATED).gate_pass(STRONG)
    assert p.survivors == ["BARE"]
    assert p.survivor_metrics["BARE"]["quality_available"] == 0


def test_a_name_passing_both_gates_is_an_error(monkeypatch):
    real = fin._run_pass

    def overlapping(gate, symbols, lookups, measurements, as_of):
        result = real(STRONG, symbols, lookups, measurements, as_of)  # both passes keep GOOD
        return fin.GatePass(gate, result.stage_counts, result.survivors, {}, result.removed)

    monkeypatch.setattr(fin, "_run_pass", overlapping)
    with pytest.raises(AssertionError, match="both"):
        run_financials(_universe(["GOOD"]), "u.json", {"GOOD": fx.Lookup("GOOD", FILES["GOOD"], fx.CACHE)},
                       created_at=CREATED)


def test_report_records_sources_freshness_and_policy():
    report = _run()
    assert report.data_sources == {"cache": 7, "fetched": 0, "stale_fallback": 0, "failed": 1}
    assert set(report.data_freshness) == set(FILES)
    assert report.policy["turnaround_rule_applied"] is False
    assert report.policy["quality_checks_required"] is False
    assert any("not point in time" in w for w in report.warnings)


def test_report_round_trips_and_never_overwrites(tmp_path):
    report = _run()
    path = save_report(report, tmp_path)
    assert path.name == "financials_2026-09-30_20261006T120500Z.json"
    loaded = load_report(path)
    assert loaded.gate_pass(STRONG).survivors == ["GOOD"]
    assert loaded.gate_pass(WEAK).stage_counts == report.gate_pass(WEAK).stage_counts
    with pytest.raises(FileExistsError):
        save_report(report, tmp_path)


# --- CLI -----------------------------------------------------------------------------------------

def test_cli_offline_on_saved_universe_report(tmp_path, capsys):
    scan_dir, fund_dir = tmp_path / "scanbot", tmp_path / "fundamentals"
    universe_path = save_universe(_universe(["GOOD", "BAD", "NOFILE"]), scan_dir)
    now = datetime.now(UTC)
    for symbol in ("GOOD", "BAD"):
        ff = FILES[symbol]
        fx.write_file(fund_dir, fx.FundamentalsFile(ff.symbol, ff.cik, ff.entity_name, now, ff.status,
                                                    ff.taxonomies, ff.facts))

    code = cli.main(["--as-of", "2026-09-30", "--report-dir", str(scan_dir), "--fundamentals-dir", str(fund_dir),
                     "--out-dir", str(tmp_path / "out"), "--offline"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert str(universe_path) in out
    assert "STRONG pass: 3 in" in out and "WEAK pass: 3 in" in out
    written = list((tmp_path / "out").glob("financials_2026-09-30_*.json"))
    data = json.loads(written[0].read_text(encoding="utf-8"))
    assert [p["survivors"] for p in data["passes"]] == [["GOOD"], ["BAD"]]
    assert data["data_sources"]["failed"] == 1


def test_cli_without_a_universe_report_is_bad_input(tmp_path, capsys):
    code = cli.main(["--as-of", "2026-09-30", "--report-dir", str(tmp_path), "--offline"])
    assert code == cli.EXIT_BAD_INPUT
    assert "no universe report" in capsys.readouterr().err
