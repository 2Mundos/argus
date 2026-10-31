from datetime import UTC, datetime

import pytest
from argus_api.routines import (
    ScheduleError,
    next_slots,
    parse_schedule,
    parse_timezone,
)

UTC_TZ = parse_timezone(None)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def slots(expr: str, after: datetime, count: int = 1, tz: str | None = None):
    return next_slots(parse_schedule(expr), parse_timezone(tz), after, count)


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("@hourly", utc(2026, 3, 4, 11, 0)),
        ("@daily", utc(2026, 3, 5, 0, 0)),
        ("@weekly", utc(2026, 3, 8, 0, 0)),  # Sunday
    ],
)
def test_aliases(expr, expected):
    assert slots(expr, utc(2026, 3, 4, 10, 30)) == [expected]


def test_lists_ranges_and_steps():
    after = utc(2026, 3, 4, 10, 0)
    assert slots("*/20 * * * *", after, 3) == [
        utc(2026, 3, 4, 10, 20),
        utc(2026, 3, 4, 10, 40),
        utc(2026, 3, 4, 11, 0),
    ]
    assert slots("5,35 9-10 * * *", after, 3) == [
        utc(2026, 3, 4, 10, 5),
        utc(2026, 3, 4, 10, 35),
        utc(2026, 3, 5, 9, 5),
    ]
    assert slots("0 8-16/4 * * *", after, 3) == [
        utc(2026, 3, 4, 12, 0),
        utc(2026, 3, 4, 16, 0),
        utc(2026, 3, 5, 8, 0),
    ]


def test_dow_seven_is_sunday():
    after = utc(2026, 3, 4, 0, 0)  # Wednesday
    assert slots("0 0 * * 7", after) == slots("0 0 * * 0", after) == [utc(2026, 3, 8)]


def test_dom_and_dow_are_ored_when_both_restricted():
    # 2026-03-04 is a Wednesday: next hits are Friday the 6th, then the 15th.
    assert slots("0 0 15 * 5", utc(2026, 3, 4), 3) == [
        utc(2026, 3, 6),
        utc(2026, 3, 13),
        utc(2026, 3, 15),
    ]


def test_dom_only_and_dow_only_are_anded_with_star():
    assert slots("0 0 15 * *", utc(2026, 3, 4)) == [utc(2026, 3, 15)]
    assert slots("0 0 * 3 1", utc(2026, 3, 4)) == [utc(2026, 3, 9)]


def test_strictly_after_exact_slot_and_seconds():
    assert slots("30 10 * * *", utc(2026, 3, 4, 10, 30)) == [utc(2026, 3, 5, 10, 30)]
    assert slots("30 10 * * *", utc(2026, 3, 4, 10, 29, 59)) == [
        utc(2026, 3, 4, 10, 30)
    ]
    assert slots("30 10 * * *", datetime(2026, 3, 4, 10, 30, 0, 1, tzinfo=UTC)) == [
        utc(2026, 3, 5, 10, 30)
    ]


def test_timezone_wall_clock_to_utc():
    after = utc(2026, 1, 10, 0, 0)
    assert slots("0 9 * * *", after, tz="America/New_York") == [utc(2026, 1, 10, 14, 0)]
    assert slots("0 9 * * *", utc(2026, 7, 10), tz="America/New_York") == [
        utc(2026, 7, 10, 13, 0)
    ]
    assert slots("0 9 * * *", utc(2026, 7, 10), tz="Europe/Lisbon") == [
        utc(2026, 7, 10, 8, 0)
    ]


def test_non_utc_after_is_accepted():
    ny = parse_timezone("America/New_York")
    after = datetime(2026, 1, 10, 8, 59, tzinfo=ny)
    assert slots("0 9 * * *", after, tz="America/New_York") == [utc(2026, 1, 10, 14, 0)]


def test_dst_gap_is_skipped():
    # 2026-03-08 02:30 does not exist in New York; the next day's slot fires.
    assert slots("30 2 * * *", utc(2026, 3, 8, 0, 0), 2, tz="America/New_York") == [
        utc(2026, 3, 9, 6, 30),
        utc(2026, 3, 10, 6, 30),
    ]


def test_dst_fold_fires_once_at_first_occurrence():
    # 2026-11-01 01:30 happens twice in New York (05:30Z EDT, 06:30Z EST).
    assert slots("30 1 * * *", utc(2026, 11, 1, 0, 0), 2, tz="America/New_York") == [
        utc(2026, 11, 1, 5, 30),
        utc(2026, 11, 2, 6, 30),
    ]


def test_count_sequence():
    assert slots("0 0 1 * *", utc(2026, 11, 15), 3) == [
        utc(2026, 12, 1),
        utc(2027, 1, 1),
        utc(2027, 2, 1),
    ]


def test_impossible_date_raises():
    with pytest.raises(ScheduleError, match="no matching time"):
        slots("0 0 31 2 *", utc(2026, 1, 1))


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "   ",
        "* * * * * *",
        "* * * *",
        "@yearly",
        "@nope",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "5-1 * * * *",
        "*/0 * * * *",
        "5/10 * * * *",
        "a * * * *",
        "-1 * * * *",
        "1,,2 * * * *",
    ],
)
def test_invalid_expressions(expr):
    with pytest.raises(ScheduleError):
        parse_schedule(expr)


def test_naive_after_and_bad_count_rejected():
    schedule = parse_schedule("@hourly")
    with pytest.raises(ScheduleError, match="timezone-aware"):
        next_slots(schedule, UTC_TZ, datetime(2026, 1, 1))
    with pytest.raises(ScheduleError, match="count"):
        next_slots(schedule, UTC_TZ, utc(2026, 1, 1), 0)


def test_timezone_parsing():
    assert parse_timezone(None).key == "UTC"
    assert parse_timezone("").key == "UTC"
    iana_name = "/".join(("Europe", "Lisbon"))
    assert parse_timezone(iana_name).key == iana_name
    for bad in ("Mars/Olympus", "../etc/" + "pass" + "wd"):
        with pytest.raises(ScheduleError, match="timezone"):
            parse_timezone(bad)
