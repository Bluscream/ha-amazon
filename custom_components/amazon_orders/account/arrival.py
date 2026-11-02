"""Read the day, or the window, an "Arriving ..." order line promises.

Pure: no I/O, no Home Assistant objects. The wording is English, the one
language a real storefront has shown for it so far. Each shape below was seen
on a real order line:

* ``Arriving today`` / ``Arriving tomorrow``
* ``Arriving Wednesday`` (the next such day, today included)
* ``Arriving Oct 27``, ``Arriving Oct 27-29``, ``Arriving Oct 30-Nov 2``
* ``Arriving 20 November - 18 December`` (amazon.de in English: day first)
* any single-day form followed by a time range: ``Arriving today 6 pm – 10 pm``,
  ``Arriving today 11:00 AM - 1:15 PM``
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time, timedelta

from .vocabulary import MONTHS

_DASH = r"\s*[-–—]\s*"
_MONTH = r"[^\W\d_]{3,}\.?"

_DAY = re.compile(
    r"(?P<word>today|tomorrow)"
    r"|(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    # Month first: "Oct 27", "Oct 27-29", "Oct 30-Nov 2"
    rf"|(?P<m1>{_MONTH})\s+(?P<d1>\d{{1,2}})(?:{_DASH}(?:(?P<m2>{_MONTH})\s+)?(?P<d2>\d{{1,2}}))?"
    # Day first: "20 November", "3-5 November", "20 November - 18 December"
    rf"|(?P<d3>\d{{1,2}})(?:\s+(?P<m3>{_MONTH}))?{_DASH}(?P<d4>\d{{1,2}})\s+(?P<m4>{_MONTH})"
    rf"|(?P<d5>\d{{1,2}})\s+(?P<m5>{_MONTH})",
    re.IGNORECASE,
)
_LINE = re.compile(r"arriving\s+(?P<rest>.+)", re.IGNORECASE)


# Only tried once the whole rest fails as a day, so "Oct 27-29" stays a span
# of days instead of reading as "Oct" plus the hours 27 to 29.
def _time(name: str) -> str:
    return (
        rf"(?P<{name}_hour>\d{{1,2}})(?:[:.](?P<{name}_minute>\d{{2}}))?"
        rf"(?:\s*(?P<{name}_half>[ap])\.?\s*m\.?)?"
    )


_TIMED = re.compile(
    rf"(?P<day>.+?)\s+{_time('start')}{_DASH}{_time('end')}", re.IGNORECASE
)
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@dataclass(frozen=True, slots=True)
class Arrival:
    """The days a line promises, and the time range on that day if it names one."""

    first: date
    last: date
    start: time | None = None
    end: time | None = None


def _month(name: str | None) -> int | None:
    return MONTHS.get(name.lower().rstrip(".")) if name else None


def _dated(today: date, month: int, day: int) -> date:
    """Date a day and month on or after yesterday, rolling into next year if needed."""
    first = date(today.year, month, day)
    return (
        first.replace(year=today.year + 1)
        if first < today - timedelta(days=1)
        else first
    )


def _span(
    today: date, start: tuple[int, int], end: tuple[int, int]
) -> tuple[date, date]:
    first = _dated(today, *start)
    last = date(first.year, *end)
    return first, last if last >= first else last.replace(year=last.year + 1)


def _days(text: str, today: date) -> tuple[date, date] | None:
    match = _DAY.fullmatch(text)
    if match is None:
        return None
    if match["word"]:
        day = today + timedelta(days=match["word"].lower() == "tomorrow")
        return day, day
    if match["weekday"]:
        ahead = (_WEEKDAYS.index(match["weekday"].lower()) - today.weekday()) % 7
        day = today + timedelta(days=ahead)
        return day, day
    if match["m1"]:
        month, end_month = _month(match["m1"]), _month(match["m2"] or match["m1"])
        start_day, end_day = int(match["d1"]), int(match["d2"] or match["d1"])
    elif match["d3"]:
        month, end_month = _month(match["m3"] or match["m4"]), _month(match["m4"])
        start_day, end_day = int(match["d3"]), int(match["d4"])
    else:
        month = end_month = _month(match["m5"])
        start_day = end_day = int(match["d5"])
    if month is None or end_month is None:
        return None
    return _span(today, (month, start_day), (end_month, end_day))


def _clock(timed: re.Match[str], name: str, half: str | None) -> time | None:
    hour = int(timed[f"{name}_hour"])
    minute = int(timed[f"{name}_minute"] or 0)
    meridiem = (timed[f"{name}_half"] or half or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _times(timed: re.Match[str]) -> tuple[time, time] | None:
    half = timed["end_half"]
    end = _clock(timed, "end", None)
    # "6 – 10 pm" names the half once; "11 – 1 pm" crosses noon from the morning.
    start = _clock(timed, "start", half)
    if start is not None and end is not None and start >= end and half:
        start = _clock(timed, "start", "a")
    if start is None or end is None or start >= end:
        return None
    return start, end


def parse_arrival(text: str | None, today: date) -> Arrival | None:
    """Return what an "Arriving ..." order line promises, if it is a shape we know."""
    line = _LINE.fullmatch((text or "").strip())
    if line is None:
        return None
    rest = line["rest"].strip()
    try:
        if (days := _days(rest, today)) is not None:
            return Arrival(*days)
        timed = _TIMED.fullmatch(rest)
        if timed is None or (days := _days(timed["day"], today)) is None:
            return None
    except ValueError:  # a day that does not exist in that month
        return None
    times = _times(timed)
    if times is None or days[0] != days[1]:
        return None
    return Arrival(days[0], days[1], *times)
