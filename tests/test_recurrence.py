"""Unit tests for the brief §5.2 effective-end derivation.

These exercise :func:`arwen.recurrence.effective_end` directly on hand-built
``icalendar`` components, with no I/O and no fixture corpus. Pruning logic
proper is out of scope here (see CLAUDE.md).
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Event

from arwen.model import AllDayDate, FloatingDateTime, Instant
from arwen.recurrence import effective_end


def test_dtend_present_is_used_directly() -> None:
    """When DTEND is given, it is the effective end, unmodified."""
    event = Event()
    event.add("DTSTART", datetime(2024, 3, 1, 9, 0, tzinfo=UTC))
    event.add("DTEND", datetime(2024, 3, 1, 10, 30, tzinfo=UTC))

    end = effective_end(event)

    assert end == Instant(datetime(2024, 3, 1, 10, 30, tzinfo=UTC))


def test_all_day_dtend_is_exclusive_and_not_shifted_further() -> None:
    """An all-day DTEND is already exclusive; effective_end must not add a day.

    This is the off-by-one trap from brief §5.1/§5.2: a naive implementation
    that always adds a day to all-day events would turn a two-day event
    (1st-2nd, DTEND 3rd) into a three-day one.
    """
    event = Event()
    event.add("DTSTART", date(2024, 3, 1))
    event.add("DTEND", date(2024, 3, 3))

    end = effective_end(event)

    assert end == AllDayDate(date(2024, 3, 3))


def test_all_day_without_dtend_or_duration_defaults_to_dtstart_plus_one_day() -> None:
    """Per RFC 5545, a DTEND-less all-day event spans exactly its DTSTART day."""
    event = Event()
    event.add("DTSTART", date(2024, 3, 1))

    end = effective_end(event)

    assert end == AllDayDate(date(2024, 3, 2))


def test_timed_without_dtend_or_duration_defaults_to_dtstart() -> None:
    """Per RFC 5545, a DTEND-less timed event ends at the same instant it starts."""
    event = Event()
    event.add("DTSTART", datetime(2024, 3, 1, 9, 0, tzinfo=UTC))

    end = effective_end(event)

    assert end == Instant(datetime(2024, 3, 1, 9, 0, tzinfo=UTC))


def test_duration_instead_of_dtend_is_added_to_dtstart() -> None:
    """DTSTART + DURATION is used when DTEND is absent but DURATION is present."""
    event = Event()
    event.add("DTSTART", datetime(2024, 3, 1, 9, 0, tzinfo=UTC))
    event.add("DURATION", timedelta(hours=2, minutes=30))

    end = effective_end(event)

    assert end == Instant(datetime(2024, 3, 1, 11, 30, tzinfo=UTC))


def test_all_day_duration_instead_of_dtend_is_added_to_dtstart() -> None:
    """DTSTART + DURATION also applies to all-day events, in whole days."""
    event = Event()
    event.add("DTSTART", date(2024, 3, 1))
    event.add("DURATION", timedelta(days=2))

    end = effective_end(event)

    assert end == AllDayDate(date(2024, 3, 3))


def test_floating_datetime_without_dtend_stays_floating() -> None:
    """A floating DTSTART with no DTEND produces a floating effective end."""
    event = Event()
    # DTZ001 twice: a naive datetime is the subject of this test, not an
    # oversight. RFC 5545 floating date-times carry no zone by definition
    # (brief §5.1), so building one is the only way to exercise them.
    event.add("DTSTART", datetime(2024, 3, 1, 9, 0))  # noqa: DTZ001

    end = effective_end(event)

    assert end == FloatingDateTime(datetime(2024, 3, 1, 9, 0))  # noqa: DTZ001


def test_tzid_dtend_and_equivalent_utc_dtend_are_the_same_instant() -> None:
    """A TZID-qualified DTEND normalizes to the same Instant as its UTC equivalent."""
    event_with_tzid = Event()
    event_with_tzid.add("DTSTART", datetime(2024, 3, 1, 9, 0, tzinfo=UTC))
    event_with_tzid.add("DTEND", datetime(2024, 3, 1, 11, 0, tzinfo=ZoneInfo("Europe/Rome")))

    event_with_z = Event()
    event_with_z.add("DTSTART", datetime(2024, 3, 1, 9, 0, tzinfo=UTC))
    event_with_z.add("DTEND", datetime(2024, 3, 1, 10, 0, tzinfo=UTC))

    assert effective_end(event_with_tzid) == effective_end(event_with_z)
