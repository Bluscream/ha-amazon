"""Tests for reading what an "Arriving ..." order line promises."""

from datetime import date, time

import pytest

from custom_components.amazon_orders.account.arrival import Arrival, parse_arrival

MONDAY = date(2026, 11, 2)
SUNDAY = date(2026, 10, 25)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Arriving tomorrow", Arrival(date(2026, 10, 26), date(2026, 10, 26))),
        ("Arriving today", Arrival(SUNDAY, SUNDAY)),
        ("Arriving Oct 27", Arrival(date(2026, 10, 27), date(2026, 10, 27))),
        ("Arriving Oct 27-29", Arrival(date(2026, 10, 27), date(2026, 10, 29))),
        ("Arriving Oct 30-Nov 2", Arrival(date(2026, 10, 30), date(2026, 11, 2))),
        ("Arriving Jan 3", Arrival(date(2027, 1, 3), date(2027, 1, 3))),
        ("Arriving Sept. 3", Arrival(date(2027, 9, 3), date(2027, 9, 3))),
        ("Arriving Foo 3", None),
        ("Arriving Feb 31", None),
        ("Morgen", None),
        ("", None),
        (None, None),
    ],
)
def test_month_first_days(text, expected):
    assert parse_arrival(text, SUNDAY) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # amazon.de in English, seen 2026-11-02
        ("Arriving Wednesday", Arrival(date(2026, 11, 4), date(2026, 11, 4))),
        ("Arriving Saturday", Arrival(date(2026, 11, 7), date(2026, 11, 7))),
        ("Arriving Monday", Arrival(MONDAY, MONDAY)),
        ("arriving SUNDAY", Arrival(date(2026, 11, 8), date(2026, 11, 8))),
        (
            "Arriving 20 November - 18 December",
            Arrival(date(2026, 11, 20), date(2026, 12, 18)),
        ),
        ("Arriving 3 November", Arrival(date(2026, 11, 3), date(2026, 11, 3))),
        ("Arriving 3-5 November", Arrival(date(2026, 11, 3), date(2026, 11, 5))),
        (
            "Arriving 28 December - 2 January",
            Arrival(date(2026, 12, 28), date(2027, 1, 2)),
        ),
        ("Arriving 31 Foo", None),
        ("Arriving 3 - 5 Foo", None),
    ],
)
def test_weekdays_and_day_first_dates(text, expected):
    assert parse_arrival(text, MONDAY) == expected


@pytest.mark.parametrize(
    "text,start,end",
    [
        ("Arriving today 6 pm – 10 pm", time(18), time(22)),
        ("Arriving today 6 – 10 pm", time(18), time(22)),
        ("Arriving today 11 – 1 pm", time(11), time(13)),
        ("Arriving today 11:00 AM - 1:15 PM", time(11), time(13, 15)),
        ("Arriving tomorrow 8 a.m. — 12 p.m.", time(8), time(12)),
        ("Arriving Wednesday 14:00 - 18:30", time(14), time(18, 30)),
        ("Arriving today 12 am - 6 am", time(0), time(6)),
    ],
)
def test_time_ranges(text, start, end):
    arrival = parse_arrival(text, MONDAY)
    assert arrival is not None
    assert (arrival.start, arrival.end) == (start, end)
    assert arrival.first == arrival.last


@pytest.mark.parametrize(
    "text",
    [
        "Arriving today 10 pm – 6 pm",  # backwards
        "Arriving today 25:00 - 26:00",  # no such hour
        "Arriving today 13 pm - 14 pm",  # no such 12-hour time
        "Arriving today 9:75 - 10:00",  # no such minute
        "Arriving 3-5 November 6 pm - 10 pm",  # a range of days has no hours
        "Arriving whenever 6 pm - 10 pm",  # not a day
        "Arriving Feb 31 6 pm - 10 pm",  # not a real day
    ],
)
def test_unreadable_time_ranges(text):
    assert parse_arrival(text, MONDAY) is None
