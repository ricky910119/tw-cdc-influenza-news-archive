"""Date slicing: boundaries, the 2024 leap year, and the future-date clamp.

The expected values here are derived from the calendar, not from running the
code and writing down what it said.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from zoneinfo import ZoneInfo

from tw_cdc_influenza_news.dates import (
    DateSlice,
    Granularity,
    build_month_slices,
    can_split,
    effective_end_date,
    month_keys_in_range,
    split_slice,
    today_in,
)


# --- half-open semantics ----------------------------------------------------


def test_month_slices_are_half_open_and_contiguous():
    slices = build_month_slices(date(2023, 1, 1), date(2023, 3, 31))

    assert [(s.start, s.end) for s in slices] == [
        (date(2023, 1, 1), date(2023, 2, 1)),
        (date(2023, 2, 1), date(2023, 3, 1)),
        (date(2023, 3, 1), date(2023, 4, 1)),
    ]
    # Each slice ends exactly where the next begins: no gap, no overlap.
    for earlier, later in zip(slices, slices[1:]):
        assert earlier.end == later.start


def test_every_day_in_the_range_belongs_to_exactly_one_slice():
    start, end = date(2023, 12, 20), date(2024, 1, 10)
    slices = build_month_slices(start, end)

    day = start
    while day <= end:
        owners = [s for s in slices if s.contains(day)]
        assert len(owners) == 1, f"{day} is in {len(owners)} slices"
        day = date.fromordinal(day.toordinal() + 1)


def test_inclusive_end_date_is_included():
    slices = build_month_slices(date(2026, 12, 1), date(2026, 12, 31))

    assert len(slices) == 1
    assert slices[0].end == date(2027, 1, 1)  # exclusive bound is the day after
    assert slices[0].last_day == date(2026, 12, 31)
    assert slices[0].contains(date(2026, 12, 31))


def test_partial_first_and_last_months_are_clipped():
    slices = build_month_slices(date(2023, 1, 10), date(2023, 3, 5))

    assert slices[0].start == date(2023, 1, 10)
    assert slices[0].end == date(2023, 2, 1)
    assert slices[-1].start == date(2023, 3, 1)
    assert slices[-1].end == date(2023, 3, 6)


def test_empty_slice_is_rejected():
    with pytest.raises(ValueError):
        DateSlice(date(2024, 1, 1), date(2024, 1, 1), Granularity.DAY)


def test_inverted_range_is_rejected():
    with pytest.raises(ValueError):
        build_month_slices(date(2024, 5, 1), date(2024, 4, 30))


# --- leap year --------------------------------------------------------------


def test_february_2024_has_29_days():
    february = build_month_slices(date(2024, 2, 1), date(2024, 2, 29))[0]

    assert february.end == date(2024, 3, 1)
    assert february.day_count == 29
    assert february.contains(date(2024, 2, 29))


def test_february_2023_has_28_days():
    february = [
        s for s in build_month_slices(date(2023, 1, 1), date(2023, 12, 31))
        if s.start.month == 2
    ][0]

    assert february.day_count == 28
    assert february.end == date(2023, 3, 1)


def test_leap_day_splits_into_day_slices_without_loss():
    february = build_month_slices(date(2024, 2, 1), date(2024, 2, 29))[0]

    weeks = split_slice(february)
    days = [d for week in weeks for d in split_slice(week)]

    assert sum(d.day_count for d in days) == 29
    assert days[-1].start == date(2024, 2, 29)
    assert days[-1].end == date(2024, 3, 1)


# --- splitting --------------------------------------------------------------


def test_month_splits_into_weeks_clipped_to_the_month():
    january = build_month_slices(date(2024, 1, 1), date(2024, 1, 31))[0]

    weeks = split_slice(january)

    assert all(w.granularity is Granularity.WEEK for w in weeks)
    assert weeks[0].start == date(2024, 1, 1)
    assert weeks[-1].end == date(2024, 2, 1)  # clipped, not spilling into February
    assert sum(w.day_count for w in weeks) == 31


def test_week_splits_into_single_days():
    week = DateSlice(date(2024, 3, 4), date(2024, 3, 11), Granularity.WEEK)

    days = split_slice(week)

    assert len(days) == 7
    assert all(d.day_count == 1 for d in days)
    assert days[0].start == date(2024, 3, 4)
    assert days[-1].start == date(2024, 3, 10)


def test_a_day_cannot_be_split_further():
    day = DateSlice(date(2024, 3, 4), date(2024, 3, 5), Granularity.DAY)

    assert can_split(day) is False
    assert split_slice(day) == [day]


def test_single_day_month_slice_is_not_splittable():
    tail = build_month_slices(date(2025, 6, 30), date(2025, 6, 30))[0]

    assert tail.day_count == 1
    assert can_split(tail) is False


# --- effective end date / no future queries ---------------------------------


def test_effective_end_clamps_a_future_request_to_today():
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    effective = effective_end_date(date(2026, 12, 31), tz_name="Asia/Taipei", now=now)

    assert effective == date(2026, 9, 17)


def test_effective_end_leaves_a_past_request_alone():
    now = datetime(2026, 9, 17, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    assert effective_end_date(date(2025, 12, 31), tz_name="Asia/Taipei", now=now) == date(
        2025, 12, 31
    )


def test_no_slice_reaches_beyond_the_effective_end():
    now = datetime(2026, 9, 17, 23, 30, tzinfo=ZoneInfo("Asia/Taipei"))
    effective = effective_end_date(date(2026, 12, 31), tz_name="Asia/Taipei", now=now)

    slices = build_month_slices(date(2023, 1, 1), effective)

    assert slices[-1].last_day == date(2026, 9, 17)
    assert all(s.last_day <= effective for s in slices)


# --- timezone ---------------------------------------------------------------


def test_today_is_read_in_taipei_not_utc():
    # 2026-09-17 23:30 in Taipei is still 15:30 UTC on the 17th, but
    # 2026-09-18 00:30 Taipei is 16:30 UTC on the 17th -- the date differs.
    late_taipei = datetime(2026, 9, 18, 0, 30, tzinfo=ZoneInfo("Asia/Taipei"))

    assert today_in("Asia/Taipei", now=late_taipei) == date(2026, 9, 18)
    assert today_in("UTC", now=late_taipei) == date(2026, 9, 17)


def test_a_naive_now_is_read_as_local_to_the_named_zone():
    naive = datetime(2026, 9, 17, 8, 0)

    assert today_in("Asia/Taipei", now=naive) == date(2026, 9, 17)


# --- month keys -------------------------------------------------------------


def test_every_month_in_the_full_project_range_is_enumerated():
    keys = month_keys_in_range(date(2023, 1, 1), date(2026, 12, 31))

    assert len(keys) == 48  # 4 years x 12 months
    assert keys[0] == "2023-01"
    assert keys[-1] == "2026-12"
    assert "2024-02" in keys


def test_month_keys_span_a_year_boundary():
    assert month_keys_in_range(date(2023, 11, 5), date(2024, 2, 3)) == [
        "2023-11",
        "2023-12",
        "2024-01",
        "2024-02",
    ]


def test_slice_month_key_matches_its_start():
    sl = DateSlice(date(2024, 1, 29), date(2024, 2, 1), Granularity.WEEK)

    assert sl.month_key == "2024-01"
