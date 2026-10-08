"""Earnings blackout reads the newest daily status for a date and each confirmed name's cached SEC facts.
Earnings within 3 sessions (as_of is session 0) remove the name; within 7 it is flagged and kept; no date
is unknown and kept. Estimated dates are labelled. One JSON and one note are written and never
overwritten. No network, no Alpaca module, no orders.
"""

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from extensions.scripts import run_earnings_blackout as cli
from extensions.status.earnings_blackout import (
    CLEAR,
    ESTIMATED,
    FILED,
    FLAGGED_KEPT,
    REMOVED,
    UNKNOWN_KEPT,
    BlackoutInputError,
    Filing,
    build_blackout,
    filings_on_file,
    latest_daily_status,
    next_earnings,
    save_blackout,
    sessions_away,
)

AS_OF = date(2026, 9, 30)
CREATED = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
STATUS_NAME = "daily_status_2026-09-30_20261008T231343Z.json"
Q2_2026 = ("2026-07-30", "10-Q", "2026-06-30")  # this year's newest report


def _status(long=("AAA",), short=("ZZZ",), created="2026-10-08T23:13:43+00:00", blocked=False):
    return {"as_of": AS_OF.isoformat(), "created_at": created, "market_label": "SIDE",
            "channel_names_blocked": blocked, "confirmed": {"long": list(long), "short": list(short)},
            "orders_placed": False, "status_version": 1}


def _facts(*filings, status="ok", fetched_at="2026-10-06T21:00:00+00:00"):
    """filings: (filed, form, end) tuples, stored as an EPS fact each."""
    rows = [{"start": "2000-01-01", "end": end, "val": 1.0, "filed": filed, "form": form}
            for filed, form, end in filings]
    return {"symbol": "X", "cik": "1", "entity_name": "X", "fetched_at": fetched_at, "status": status,
            "taxonomies": ["us-gaap"], "facts": {"us-gaap": {"EarningsPerShareDiluted": {"USD/shares": rows}}},
            "cache_version": 1}


def _write(folder, name, data):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(data), encoding="utf-8")


def _blackout(tmp_path, facts_by_symbol, status=None):
    for symbol, data in facts_by_symbol.items():
        _write(tmp_path / "fundamentals", f"{symbol}.json", data)
    return build_blackout(AS_OF, (Path(STATUS_NAME), status or _status()), tmp_path / "fundamentals",
                          created_at=CREATED)


@pytest.mark.unit
def test_sessions_count_forward_from_as_of():
    assert sessions_away(AS_OF, AS_OF) == 0
    assert sessions_away(AS_OF, date(2026, 10, 1)) == 1
    assert sessions_away(AS_OF, date(2026, 10, 5)) == 3  # Thu, Fri, Mon
    assert sessions_away(AS_OF, date(2026, 10, 3)) == 3  # a Saturday counts as Monday
    assert sessions_away(AS_OF, date(2026, 10, 9)) == 7
    assert sessions_away(AS_OF, date(2026, 10, 12)) == 8
    assert sessions_away(date(2026, 9, 4), date(2026, 9, 8)) == 1  # Labor Day is skipped


@pytest.mark.unit
def test_filings_are_grouped_by_filed_date_and_form():
    data = _facts(("2025-10-30", "10-Q", "2025-09-30"), ("2025-10-30", "10-Q", "2024-09-30"),
                  ("2025-11-02", "8-K", "2025-09-30"))
    assert filings_on_file(data) == [Filing(date(2025, 10, 30), "10-Q", date(2025, 9, 30))]


@pytest.mark.unit
def test_a_filing_on_or_after_as_of_is_the_filed_date():
    filings = [Filing(date(2026, 7, 30), "10-Q", date(2026, 6, 30)), Filing(date(2026, 10, 2), "10-Q", date(2026, 9, 30))]
    day, source, basis = next_earnings(AS_OF, filings)
    assert day == date(2026, 10, 2) and source == FILED and "filed 2026-10-02" in basis


@pytest.mark.unit
def test_estimate_skips_periods_already_reported_and_is_labelled():
    filings = [
        Filing(date(2025, 9, 10), "10-Q", date(2025, 8, 31)),   # anniversary before as_of
        Filing(date(2025, 10, 2), "10-Q", date(2025, 9, 15)),   # period already reported this year
        Filing(date(2025, 10, 16), "10-Q", date(2025, 9, 30)),  # the next one
        Filing(date(2026, 9, 20), "10-Q", date(2026, 9, 15)),
    ]
    day, source, basis = next_earnings(AS_OF, filings)
    assert day == date(2026, 10, 16) and source == ESTIMATED and basis.startswith("estimated")


@pytest.mark.unit
def test_estimate_skips_a_missing_filing_whose_anniversary_has_passed():
    filings = [Filing(date(2025, 7, 29), "10-Q", date(2025, 6, 30)), Filing(date(2025, 10, 30), "10-Q", date(2025, 9, 30)),
               Filing(date(2026, 4, 30), "10-Q", date(2026, 3, 31))]
    day, source, basis = next_earnings(AS_OF, filings)
    assert (day, source) == (date(2026, 10, 30), ESTIMATED)
    assert "assumes reported, though not in the cache: period ending about 2026-06-30" in basis


@pytest.mark.unit
def test_no_year_ago_filing_for_the_next_quarter_gives_no_date():
    """A recent listing: last year's annual report must not stand in for the coming quarter."""
    filings = [Filing(date(2026, 3, 19), "10-K", date(2025, 12, 31)), Filing(date(2026, 8, 13), "10-Q", date(2026, 6, 30))]
    day, source, basis = next_earnings(AS_OF, filings)
    assert day is None and source is None and "period after 2026-06-30" in basis


@pytest.mark.unit
def test_no_history_gives_no_date():
    assert next_earnings(AS_OF, []) == (None, None, "no 10-Q or 10-K in the cache")
    day, source, _ = next_earnings(AS_OF, [Filing(date(2026, 8, 13), "10-Q", date(2026, 6, 30))])
    assert day is None and source is None


@pytest.mark.unit
def test_the_three_and_seven_session_rules(tmp_path):
    status = _status(long=("ZERO", "THREE", "FOUR", "SEVEN"), short=("EIGHT", "NONE"))
    b = _blackout(tmp_path, {
        "ZERO": _facts(Q2_2026, ("2025-09-30", "10-Q", "2025-09-15")),       # estimated 2026-09-30, session 0
        "THREE": _facts(("2026-10-05", "10-Q", "2026-09-30")),      # filed, session 3
        "FOUR": _facts(Q2_2026, ("2025-10-06", "10-Q", "2025-09-30")),  # estimated, session 4
        "SEVEN": _facts(Q2_2026, ("2025-10-09", "10-Q", "2025-09-30")),  # estimated, session 7
        "EIGHT": _facts(Q2_2026, ("2025-10-12", "10-Q", "2025-09-30")),  # estimated, session 8
        "NONE": _facts(),
    }, status)
    by = {c.symbol: c for c in b.names}
    assert by["ZERO"].sessions_away == 0 and by["ZERO"].result == REMOVED and by["ZERO"].date_source == ESTIMATED
    assert by["THREE"].result == REMOVED and by["THREE"].date_source == FILED
    assert by["FOUR"].result == FLAGGED_KEPT and by["SEVEN"].result == FLAGGED_KEPT
    assert by["EIGHT"].result == CLEAR and by["NONE"].result == UNKNOWN_KEPT
    assert b.removed == ["ZERO", "THREE"] and b.flagged_kept == ["FOUR", "SEVEN"] and b.unknown_kept == ["NONE"]
    assert b.allowed == {"long": ["FOUR", "SEVEN"], "short": ["EIGHT", "NONE"]}
    assert b.orders_placed is False and b.fetched is False
    assert any("estimated dates" in w for w in b.warnings)


@pytest.mark.unit
def test_missing_or_bad_cache_is_unknown_and_kept(tmp_path):
    (tmp_path / "fundamentals").mkdir()
    (tmp_path / "fundamentals" / "BAD.json").write_text("{not json", encoding="utf-8")
    b = _blackout(tmp_path, {"NOFACT": _facts(status="no_facts")},
                  _status(long=("GONE", "BAD"), short=("NOFACT",)))
    assert [c.result for c in b.names] == [UNKNOWN_KEPT] * 3
    assert b.allowed == {"long": ["GONE", "BAD"], "short": ["NOFACT"]} and b.removed == []
    assert b.names[0].basis == "no fundamentals cache file"


@pytest.mark.unit
def test_newest_daily_status_is_used_and_missing_status_stops(tmp_path):
    with pytest.raises(BlackoutInputError, match="no daily status"):
        latest_daily_status(AS_OF, tmp_path)
    _write(tmp_path, STATUS_NAME, _status(long=("OLD",)))
    _write(tmp_path, "daily_status_2026-09-30_00000000T000000Z.json",
           _status(long=("NEW",), created="2026-10-09T00:00:00+00:00"))
    _, data = latest_daily_status(AS_OF, tmp_path)
    assert data["confirmed"]["long"] == ["NEW"]


@pytest.mark.unit
def test_save_writes_json_and_note_and_never_overwrites(tmp_path):
    b = _blackout(tmp_path, {"AAA": _facts(Q2_2026, ("2025-10-06", "10-Q", "2025-09-30")), "ZZZ": _facts()})
    json_path, note_path = save_blackout(b, tmp_path / "out")
    assert json_path.name == "earnings_blackout_2026-09-30_20261008T120000Z.json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["as_of"] == "2026-09-30" and data["orders_placed"] is False and data["fetched"] is False
    assert data["names"][0]["date_source"] == ESTIMATED
    note = note_path.read_text(encoding="utf-8")
    assert "| AAA | long | 2026-10-06 | estimated | 4 | flagged_kept |" in note and "no order placed" in note
    with pytest.raises(FileExistsError):
        save_blackout(b, tmp_path / "out")


@pytest.mark.unit
def test_cli_prints_and_writes(tmp_path, capsys):
    _write(tmp_path / "status", STATUS_NAME, _status())
    _write(tmp_path / "fundamentals", "AAA.json", _facts(Q2_2026, ("2025-10-01", "10-Q", "2025-09-30")))
    out_dir = tmp_path / "out"
    code = cli.main(["--as-of", "2026-09-30", "--status-dir", str(tmp_path / "status"),
                     "--fundamentals-dir", str(tmp_path / "fundamentals"), "--out-dir", str(out_dir)])
    assert code == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "removed: AAA" in printed and "unknown and kept: ZZZ" in printed and "no order placed" in printed
    assert len(list(out_dir.glob("earnings_blackout_2026-09-30_*.json"))) == 1
    assert len(list(out_dir.glob("earnings_blackout_2026-09-30_*.md"))) == 1


@pytest.mark.unit
def test_cli_without_a_status_writes_nothing(tmp_path, capsys):
    out_dir = tmp_path / "out"
    code = cli.main(["--as-of", "2026-09-30", "--status-dir", str(tmp_path), "--out-dir", str(out_dir)])
    assert code == cli.EXIT_BAD_INPUT and not out_dir.exists()
    assert "nothing written" in capsys.readouterr().err


@pytest.mark.unit
def test_earnings_blackout_imports_no_alpaca_or_http_module():
    """In a fresh interpreter, loading the module and its script leaves no alpaca or requests module loaded."""
    code = (
        "import sys, extensions.status.earnings_blackout, extensions.scripts.run_earnings_blackout; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in ('alpaca', 'requests')))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"
