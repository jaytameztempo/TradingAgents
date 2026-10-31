"""NYSE trading days, from the exchange's published holiday rules (no network, no extra package).

Full-day holidays: New Year's Day, Martin Luther King Jr. Day, Washington's Birthday, Good Friday,
Memorial Day, Juneteenth (from 2022), Independence Day, Labor Day, Thanksgiving and Christmas. A holiday on
a Saturday is observed the Friday before and one on a Sunday the Monday after, except New Year's Day on a
Saturday, which is not made up (NYSE Rule 7.2).

Early closes (1:00 PM New York) are trading days: the day after Thanksgiving, and July 3 and December 24
when they are trading days. One-off closures (a national day of mourning, a storm) are not predictable
from rules and are not here.

Standard library only, so paper_orders.trade_window (and through it the sender) and the UI share it.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from functools import cache


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th (1-based) weekday (Mon=0) of a month; n=-1 is the last."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@cache
def holidays(year: int) -> frozenset[date]:
    days = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter(year) - timedelta(days=2),  # Good Friday
        _nth_weekday(year, 5, 0, -1),  # Memorial Day
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # a Saturday New Year's Day is not observed on Dec 31
        days.add(_observed(new_year))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    return frozenset(days)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def previous_trading_day(d: date) -> date:
    """The last trading day strictly before d."""
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


EARLY_CLOSE = time(13, 0)


def early_close(d: date) -> time | None:
    """The 1:00 PM close on an early-close trading day, else None."""
    if not is_trading_day(d):
        return None
    if d == _nth_weekday(d.year, 11, 3, 4) + timedelta(days=1):  # day after Thanksgiving
        return EARLY_CLOSE
    if (d.month, d.day) in ((7, 3), (12, 24)):
        return EARLY_CLOSE
    return None


def holiday_name(d: date) -> str | None:
    """A short label for a full-day holiday (or a plain weekend), for messages."""
    if d.weekday() >= 5:
        return "weekend"
    return "market holiday" if d in holidays(d.year) else None
