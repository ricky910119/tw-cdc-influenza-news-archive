"""Date-range slicing and timezone handling.

Two rules drive everything here:

1. Every slice is half-open: ``[start, end)``. A day belongs to exactly one
   slice, so no boundary date is ever queried twice or skipped.
2. We never query the future. The *effective* end date is
   ``min(requested_end, today_in_display_timezone)``.

Callers speak in INCLUSIVE dates (``--end-date 2026-12-31`` means "through
31 December"). :func:`build_month_slices` converts that to the exclusive
bound exactly once, at the boundary.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Taipei"


class Granularity(StrEnum):
    MONTH = "month"
    WEEK = "week"
    DAY = "day"


#: Granularity -> the granularity a truncated slice is split into.
NEXT_GRANULARITY: dict[Granularity, Granularity | None] = {
    Granularity.MONTH: Granularity.WEEK,
    Granularity.WEEK: Granularity.DAY,
    Granularity.DAY: None,
}


def get_timezone(name: str = DEFAULT_TIMEZONE) -> ZoneInfo:
    return ZoneInfo(name)


def now_in(tz_name: str = DEFAULT_TIMEZONE) -> datetime:
    return datetime.now(tz=get_timezone(tz_name))


def today_in(tz_name: str = DEFAULT_TIMEZONE, *, now: datetime | None = None) -> date:
    """Today's calendar date in ``tz_name``.

    ``now`` is injectable so tests can pin "today" without patching a clock.
    A naive ``now`` is read as already being in ``tz_name``.
    """
    if now is None:
        now = now_in(tz_name)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=get_timezone(tz_name))
    return now.astimezone(get_timezone(tz_name)).date()


def effective_end_date(
    requested_end: date,
    *,
    tz_name: str = DEFAULT_TIMEZONE,
    now: datetime | None = None,
) -> date:
    """The last date we are allowed to query, inclusive.

    While the requested range runs into the future -- 2026-12-31 asked for
    in September 2026 -- the effective end is today in the display
    timezone. Sending queries for future dates wastes quota and invites
    provider errors that look like coverage gaps.
    """
    return min(requested_end, today_in(tz_name, now=now))


def utc_now_iso() -> str:
    """Current instant as an ISO 8601 string with an explicit offset."""
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class DateSlice:
    """A half-open ``[start, end)`` date window."""

    start: date
    end: date  # EXCLUSIVE
    granularity: Granularity

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"empty or inverted slice: [{self.start}, {self.end})")

    @property
    def last_day(self) -> date:
        """The last date INSIDE the slice, for providers that want inclusive bounds."""
        return self.end - timedelta(days=1)

    @property
    def day_count(self) -> int:
        return (self.end - self.start).days

    @property
    def key(self) -> str:
        return f"{self.granularity.value}:{self.start.isoformat()}:{self.end.isoformat()}"

    @property
    def month_key(self) -> str:
        """``YYYY-MM`` of the slice's first day, for coverage roll-up."""
        return f"{self.start.year:04d}-{self.start.month:02d}"

    def contains(self, day: date) -> bool:
        return self.start <= day < self.end

    def to_dict(self) -> dict[str, str | int]:
        return {
            "granularity": self.granularity.value,
            "start": self.start.isoformat(),
            "end_exclusive": self.end.isoformat(),
            "last_day": self.last_day.isoformat(),
            "day_count": self.day_count,
        }


def _first_of_next_month(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def build_month_slices(
    start_date: date,
    end_date_inclusive: date,
) -> list[DateSlice]:
    """Split ``[start_date, end_date_inclusive]`` into month slices.

    The first and last slices are clipped to the requested bounds, so a run
    starting on the 10th produces a first slice of ``[10th, 1st of next)``
    rather than a full month. Month lengths come from the calendar, so
    2024-02 is 29 days without a special case.
    """
    if end_date_inclusive < start_date:
        raise ValueError(
            f"end date {end_date_inclusive} precedes start date {start_date}"
        )
    end_exclusive = end_date_inclusive + timedelta(days=1)

    slices: list[DateSlice] = []
    cursor = start_date
    while cursor < end_exclusive:
        boundary = min(_first_of_next_month(cursor), end_exclusive)
        slices.append(DateSlice(cursor, boundary, Granularity.MONTH))
        cursor = boundary
    return slices


def split_slice(parent: DateSlice) -> list[DateSlice]:
    """Split a slice one level finer, clipped to the parent's own bounds.

    A day slice has no finer level and is returned unchanged wrapped in a
    single-element list; callers check :func:`can_split` first and record a
    completeness warning when they cannot.
    """
    child = NEXT_GRANULARITY[parent.granularity]
    if child is None:
        return [parent]

    step = timedelta(days=7 if child is Granularity.WEEK else 1)
    slices: list[DateSlice] = []
    cursor = parent.start
    while cursor < parent.end:
        boundary = min(cursor + step, parent.end)
        slices.append(DateSlice(cursor, boundary, child))
        cursor = boundary
    return slices


def can_split(sl: DateSlice) -> bool:
    return NEXT_GRANULARITY[sl.granularity] is not None and sl.day_count > 1


def month_keys_in_range(start_date: date, end_date_inclusive: date) -> list[str]:
    """Every ``YYYY-MM`` touched by the range, so no month is silently absent.

    The coverage report needs a row for every month in the REQUESTED range,
    including months in which nothing ran -- an absent row and a row saying
    "not attempted" look identical to a reader only if we omit it.
    """
    keys: list[str] = []
    year, month = start_date.year, start_date.month
    while (year, month) <= (end_date_inclusive.year, end_date_inclusive.month):
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return keys


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]
