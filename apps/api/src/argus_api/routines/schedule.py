"""Pure cron schedule computation (docs/specs/routines.spec.md 3.2).

Five fields (minute hour day-of-month month day-of-week) with ``*``, numbers,
comma lists, ``a-b`` ranges and ``*/n`` / ``a-b/n`` steps, plus the aliases
``@hourly``, ``@daily`` and ``@weekly``. Day-of-week is 0-7 (0 and 7 are Sunday).
When both day-of-month and day-of-week are restricted, a day matches if either
matches (standard cron).

Slots are computed on the routine's local wall clock at minute resolution and
returned in UTC. Nothing here reads the clock or the database.

DST: a local time that does not exist (spring-forward gap) is skipped; an
ambiguous local time (fall-back) fires once, at its first occurrence.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Longest look-ahead before a schedule is declared unsatisfiable (e.g. ``0 0 31 2 *``).
_MAX_DAYS = 366 * 5

_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0"}
# (name, low, high) per field, in cron order. Day-of-week accepts 7 as Sunday.
_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
)


class ScheduleError(ValueError):
    """An invalid cron expression, timezone, or schedule query."""


@dataclass(frozen=True)
class Schedule:
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]  # 0 = Sunday
    dom_restricted: bool
    dow_restricted: bool

    def matches_day(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        dom = day.day in self.days_of_month
        dow = (day.weekday() + 1) % 7 in self.days_of_week
        if self.dom_restricted and self.dow_restricted:
            return dom or dow
        return dom and dow


def _number(text: str, name: str, low: int, high: int) -> int:
    if not text.isascii() or not text.isdigit():
        raise ScheduleError(f"invalid {name} value {text!r}")
    value = int(text)
    if not low <= value <= high:
        raise ScheduleError(f"{name} value {value} out of range {low}-{high}")
    return value


def _field(text: str, name: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            step = _number(step_text, f"{name} step", 1, high)
        if base == "*":
            # ``*`` on day-of-week means 0-6, so 7 does not alias Sunday twice.
            start, end = low, 6 if name == "day-of-week" else high
        elif "-" in base:
            first, _, last = base.partition("-")
            start, end = _number(first, name, low, high), _number(last, name, low, high)
            if start > end:
                raise ScheduleError(f"{name} range {base!r} is reversed")
        elif slash:
            raise ScheduleError(f"{name} step needs '*' or a range, got {part!r}")
        else:
            start = end = _number(base, name, low, high)
        values.update(range(start, end + 1, step))
    return values


def parse_schedule(expr: str) -> Schedule:
    text = _ALIASES.get(expr.strip(), expr).strip()
    if text.startswith("@"):
        raise ScheduleError(f"unknown schedule alias {expr.strip()!r}")
    parts = text.split()
    if len(parts) != len(_FIELDS):
        raise ScheduleError(f"expected 5 cron fields, got {len(parts)}")
    minute, hour, dom, month, dow = (
        _field(part, name, low, high)
        for part, (name, low, high) in zip(parts, _FIELDS, strict=True)
    )
    return Schedule(
        minutes=tuple(sorted(minute)),
        hours=tuple(sorted(hour)),
        days_of_month=frozenset(dom),
        months=frozenset(month),
        days_of_week=frozenset(d % 7 for d in dow),
        dom_restricted=not parts[2].startswith("*"),
        dow_restricted=not parts[4].startswith("*"),
    )


def parse_timezone(name: str | None) -> ZoneInfo:
    if not name or not name.strip():
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ScheduleError(f"unknown timezone {name!r}") from exc


def next_slots(
    schedule: Schedule, tz: ZoneInfo, after: datetime, count: int = 1
) -> list[datetime]:
    """The next ``count`` slots strictly after ``after``, as aware UTC datetimes."""
    if after.tzinfo is None or after.utcoffset() is None:
        raise ScheduleError("'after' must be timezone-aware")
    if count < 1:
        raise ScheduleError("count must be at least 1")
    after_utc = after.astimezone(UTC)
    day = after_utc.astimezone(tz).date()
    slots: list[datetime] = []
    for _ in range(_MAX_DAYS):
        if schedule.matches_day(day):
            for hour in schedule.hours:
                for minute in schedule.minutes:
                    local = datetime(
                        day.year, day.month, day.day, hour, minute, tzinfo=tz
                    )
                    slot = local.astimezone(UTC)
                    # A nonexistent wall time does not survive the round trip.
                    if slot <= after_utc or slot.astimezone(tz).replace(
                        tzinfo=None
                    ) != (local.replace(tzinfo=None)):
                        continue
                    slots.append(slot)
                    if len(slots) == count:
                        return slots
        day += timedelta(days=1)
    raise ScheduleError("schedule has no matching time within 5 years")
