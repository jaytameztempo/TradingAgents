"""Earnings blackout for the confirmed SIDE names in one daily status. It places no orders.

It reads the newest daily status for the date and, for each confirmed long and
short name, that symbol's cached SEC EDGAR company-facts file under
~/.tradingagents/fundamentals/. Nothing is fetched: a name without a usable
cache file is ``unknown`` and kept.

The cache holds the filed dates of past 10-Q and 10-K reports, not earnings
announcement dates, so each name's next earnings date is one of:

- ``filed``: a 10-Q or 10-K in the cache filed on or after ``as_of``; the
  cache only sees filings up to its ``fetched_at``.
- ``estimated``: no such filing, so one year after last year's filing for the
  fiscal quarter after the newest period on file. A quarter whose anniversary
  is already before ``as_of`` is taken as reported (the cache can miss a
  filing) and the next quarter is used. No year-ago filing for the next quarter
  means no date. A 10-Q is often filed a day or more after the results are
  announced, so an estimate can be late.
- none: ``unknown``, flagged and kept.

Sessions are NYSE sessions counted forward from ``as_of``, which is session 0.
A date that is not a session counts as the next session. Within
``REMOVE_SESSIONS`` the name is removed from the allowed list; within
``FLAG_SESSIONS`` it is flagged and kept. Every file is read with plain
``json``; no Alpaca module is loaded. The scans, router, bots and the daily
status are not touched.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from extensions.status.daily_status import SIDES, default_status_dir

REPORT_VERSION = 1
DAILY_STATUS_VERSION = 1
REMOVE_SESSIONS = 3
FLAG_SESSIONS = 7
REPORT_FORMS = ("10-Q", "10-K")
# Last year's period counts as already reported when it ends within this many
# days of this year's newest period, one year back; 52/53-week years drift.
PERIOD_TOLERANCE_DAYS = 10
QUARTER = timedelta(days=92)
SOURCE = "SEC EDGAR company facts cache (~/.tradingagents/fundamentals); no fetch"

FILED, ESTIMATED = "filed", "estimated"
REMOVED, FLAGGED_KEPT, CLEAR, UNKNOWN_KEPT = "removed", "flagged_kept", "clear", "unknown_kept"

# NYSE full-day closures. Session counts outside these years stop the run.
NYSE_HOLIDAYS = {
    2025: ("2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
           "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25"),
    2026: ("2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
           "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"),
    2027: ("2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
           "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"),
}
_HOLIDAYS = {date.fromisoformat(d) for days in NYSE_HOLIDAYS.values() for d in days}


class BlackoutInputError(RuntimeError):
    """No daily status for the date, or a saved file that cannot be read or does not make sense."""


def default_fundamentals_dir() -> Path:
    return Path.home() / ".tradingagents" / "fundamentals"


def is_session(day: date) -> bool:
    if day.year not in NYSE_HOLIDAYS:
        raise BlackoutInputError(f"no NYSE holiday list for {day.year}; sessions cannot be counted")
    return day.weekday() < 5 and day not in _HOLIDAYS


def sessions_away(as_of: date, day: date) -> int:
    """Sessions after as_of up to the session day falls on (or the next one). as_of itself is 0."""
    if day <= as_of:
        return 0
    count, current = 0, as_of
    while True:
        current += timedelta(days=1)
        if is_session(current):
            count += 1
            if current >= day:
                return count


def _one_year_later(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:  # Feb 29
        return day.replace(year=day.year + 1, day=28)


@dataclass(frozen=True)
class Filing:
    filed: date
    form: str
    period_end: date


def filings_on_file(facts_file: dict) -> list[Filing]:
    """Each 10-Q or 10-K once, by filed date and form, with the latest period end it reports."""
    ends: dict[tuple[date, str], date] = {}
    for units in facts_file.get("facts", {}).values():
        for by_unit in units.values():
            for facts in by_unit.values():
                for fact in facts:
                    if fact.get("form") not in REPORT_FORMS or not fact.get("filed") or not fact.get("end"):
                        continue
                    key = (date.fromisoformat(fact["filed"]), fact["form"])
                    end = date.fromisoformat(fact["end"])
                    ends[key] = max(end, ends.get(key, end))
    return sorted((Filing(filed, form, end) for (filed, form), end in ends.items()), key=lambda f: f.filed)


def next_earnings(as_of: date, filings: list[Filing]) -> tuple[date | None, str | None, str]:
    """(date, source, basis) of the next report on or after as_of, or (None, None, why)."""
    after = [f for f in filings if f.filed >= as_of]
    if after:
        f = after[0]
        return f.filed, FILED, f"{f.form} filed {f.filed} for the period ending {f.period_end}"
    before = [f for f in filings if f.filed < as_of]
    if not before:
        return None, None, "no 10-Q or 10-K in the cache"
    end = max(f.period_end for f in before)
    tolerance = timedelta(days=PERIOD_TOLERANCE_DAYS)
    assumed: list[str] = []
    # Walk forward one fiscal quarter at a time from the newest period on file.
    for _ in range(4):
        next_period = [f for f in before
                       if end + tolerance < _one_year_later(f.period_end) <= end + QUARTER + tolerance]
        if not next_period:
            return None, None, f"no filing a year earlier for the period after {end}"
        f = min(next_period, key=lambda f: f.filed)
        day = _one_year_later(f.filed)
        if day >= as_of:
            note = f"; assumes reported, though not in the cache: {', '.join(assumed)}" if assumed else ""
            return day, ESTIMATED, (f"estimated: one year after the {f.form} filed {f.filed} "
                                    f"for the period ending {f.period_end}{note}")
        end = _one_year_later(f.period_end)
        assumed.append(f"period ending about {end}")
    return None, None, f"no estimate on or after {as_of} within four quarters"


def result_for(sessions: int | None) -> str:
    if sessions is None:
        return UNKNOWN_KEPT
    if sessions <= REMOVE_SESSIONS:
        return REMOVED
    if sessions <= FLAG_SESSIONS:
        return FLAGGED_KEPT
    return CLEAR


@dataclass(frozen=True)
class NameCheck:
    symbol: str
    side: str
    earnings_date: str | None
    date_source: str | None  # filed, estimated, or None
    basis: str
    sessions_away: int | None
    result: str
    fundamentals_file: str | None
    fetched_at: str | None


def check_name(symbol: str, side: str, as_of: date, fundamentals_dir: Path) -> tuple[NameCheck, list[str]]:
    path = fundamentals_dir / f"{symbol}.json"
    warnings: list[str] = []

    def unknown(basis, fetched_at=None):
        return NameCheck(symbol, side, None, None, basis, None, UNKNOWN_KEPT,
                         str(path) if path.exists() else None, fetched_at), warnings

    if not path.exists():
        return unknown("no fundamentals cache file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = data["fetched_at"]
        fetched_day = datetime.fromisoformat(fetched_at).date()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return unknown(f"fundamentals cache file cannot be read: {exc}")
    if data.get("status") != "ok":
        return unknown(f"fundamentals cache status is {data.get('status')!r}", fetched_at)
    try:
        filings = filings_on_file(data)
    except (ValueError, TypeError, AttributeError) as exc:
        return unknown(f"fundamentals cache facts cannot be read: {exc}", fetched_at)
    if fetched_day < as_of:
        warnings.append(f"{symbol}: the cache was fetched {fetched_day}, before as_of; "
                        "a filing on or after as_of cannot be in it")

    day, source, basis = next_earnings(as_of, filings)
    if day is None:
        return unknown(basis, fetched_at)
    sessions = sessions_away(as_of, day)
    return NameCheck(symbol, side, day.isoformat(), source, basis, sessions, result_for(sessions),
                     str(path), fetched_at), warnings


@dataclass(frozen=True)
class EarningsBlackout:
    as_of: date
    created_at: datetime
    daily_status_file: str
    market_label: str
    channel_names_blocked: bool
    names: list[NameCheck]
    allowed: dict[str, list[str]]  # long, short: the confirmed names minus the removed ones
    removed: list[str]
    flagged_kept: list[str]
    unknown_kept: list[str]
    source: str = SOURCE
    rules: dict = field(default_factory=lambda: {
        "session_0": "as_of",
        "removed_within_sessions": REMOVE_SESSIONS,
        "flagged_kept_within_sessions": FLAG_SESSIONS,
        "no_date": "flagged unknown and kept",
    })
    warnings: list[str] = field(default_factory=list)
    fetched: bool = False
    orders_placed: bool = False
    report_version: int = REPORT_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data


def latest_daily_status(as_of: date, status_dir: str | Path | None = None) -> tuple[Path, dict]:
    folder = Path(status_dir) if status_dir is not None else default_status_dir()
    found = []
    for path in sorted(folder.glob(f"daily_status_{as_of}_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(data["created_at"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BlackoutInputError(f"cannot read daily status {path}: {exc}") from exc
        if data.get("as_of") == as_of.isoformat():
            found.append((created, path, data))
    if not found:
        raise BlackoutInputError(f"no daily status for {as_of} in {folder}")
    _, path, data = max(found, key=lambda item: item[0])
    if data.get("status_version") != DAILY_STATUS_VERSION:
        raise BlackoutInputError(f"daily status {path} has unsupported status_version {data.get('status_version')!r}")
    confirmed = data.get("confirmed")
    if not isinstance(confirmed, dict) or not all(isinstance(confirmed.get(side), list) for side in SIDES):
        raise BlackoutInputError(f"daily status {path} has no confirmed long and short lists")
    return path, data


def build_blackout(
    as_of: date,
    status: tuple[Path, dict],
    fundamentals_dir: str | Path | None = None,
    created_at: datetime | None = None,
) -> EarningsBlackout:
    """Check each confirmed name of the daily status. Reads the cache only; writes nothing."""
    folder = Path(fundamentals_dir) if fundamentals_dir is not None else default_fundamentals_dir()
    status_path, s = status
    is_session(as_of)  # stops the run when the year has no holiday list
    checks: list[NameCheck] = []
    warnings: list[str] = []
    for side in SIDES:
        for symbol in s["confirmed"][side]:
            check, name_warnings = check_name(symbol, side, as_of, folder)
            checks.append(check)
            warnings.extend(name_warnings)

    if s.get("channel_names_blocked"):
        warnings.append("the daily status blocks the channel names; there are no confirmed names to check")
    if any(c.date_source == ESTIMATED for c in checks):
        warnings.append("estimated dates come from last year's 10-Q/10-K filed dates, not announced earnings "
                        "dates; a 10-Q is often filed a day or more after the results")
    fetched = sorted({c.fetched_at[:10] for c in checks if c.fetched_at})
    if fetched and fetched[-1] > as_of.isoformat():
        warnings.append(f"the fundamentals cache was fetched {', '.join(fetched)}, after as_of: "
                        "a filed date on or after as_of is not point in time")

    removed = [c.symbol for c in checks if c.result == REMOVED]
    return EarningsBlackout(
        as_of=as_of,
        created_at=created_at or datetime.now(UTC),
        daily_status_file=str(status_path),
        market_label=s.get("market_label", ""),
        channel_names_blocked=bool(s.get("channel_names_blocked")),
        names=checks,
        allowed={side: [n for n in s["confirmed"][side] if n not in removed] for side in SIDES},
        removed=removed,
        flagged_kept=[c.symbol for c in checks if c.result == FLAGGED_KEPT],
        unknown_kept=[c.symbol for c in checks if c.result == UNKNOWN_KEPT],
        warnings=list(dict.fromkeys(warnings)),
    )


def blackout_for_date(
    as_of: date,
    status_dir: str | Path | None = None,
    fundamentals_dir: str | Path | None = None,
) -> EarningsBlackout:
    return build_blackout(as_of, latest_daily_status(as_of, status_dir), fundamentals_dir)


def status_note(b: EarningsBlackout) -> str:
    """A short markdown note for the operator."""
    lines = [
        f"# Earnings blackout, {b.as_of}",
        "",
        f"Created {b.created_at:%Y-%m-%d %H:%M} UTC from `{Path(b.daily_status_file).name}` "
        f"(market label {b.market_label}). Read only: nothing fetched, no order placed.",
        "",
        f"- Source: {b.source}",
        f"- Rules: session 0 is {b.as_of}. Earnings within {REMOVE_SESSIONS} sessions: removed from the "
        f"allowed list. Within {FLAG_SESSIONS} sessions: flagged and kept. No date: flagged unknown and kept.",
        f"- Removed: {', '.join(b.removed) or 'none'}",
        f"- Flagged and kept: {', '.join(b.flagged_kept) or 'none'}",
        f"- Unknown and kept: {', '.join(b.unknown_kept) or 'none'}",
        "",
        "## Allowed names",
        "",
    ]
    lines += [f"- {side}: {' '.join(b.allowed[side]) or '-'}" for side in SIDES]
    lines += [
        "",
        "## Names",
        "",
        "| Symbol | Side | Earnings date | Date source | Sessions away | Result | Basis |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in b.names:
        away = "-" if c.sessions_away is None else str(c.sessions_away)
        lines.append(f"| {c.symbol} | {c.side} | {c.earnings_date or '-'} | {c.date_source or '-'} | "
                     f"{away} | {c.result} | {c.basis} |")
    if b.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in b.warnings]
    return "\n".join(lines) + "\n"


def save_blackout(b: EarningsBlackout, out_dir: str | Path | None = None) -> tuple[Path, Path]:
    """Write the JSON and the note as new files and return their paths. Existing files are never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_status_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"earnings_blackout_{b.as_of}_{b.created_at:%Y%m%dT%H%M%SZ}"
    json_path, note_path = folder / f"{stem}.json", folder / f"{stem}.md"
    if json_path.exists() or note_path.exists():
        raise FileExistsError(f"{stem} already exists in {folder}")
    with json_path.open("x", encoding="utf-8") as fh:
        json.dump(b.to_dict(), fh, indent=2)
        fh.write("\n")
    with note_path.open("x", encoding="utf-8") as fh:
        fh.write(status_note(b))
    return json_path, note_path
