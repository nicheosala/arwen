"""Unit tests for the brief §5.3 non-recurring classification.

Exercises :func:`arwen.recurrence.classify_non_recurring` against real
``.ics`` fixtures under ``tests/fixtures/``, covering all three actions from
the brief's table plus the all-day exclusive-``DTEND`` boundary case.
"""

from datetime import UTC, date
from pathlib import Path
from zoneinfo import ZoneInfo

from icalendar import Calendar, Component

from arwen.model import Action
from arwen.recurrence import classify_non_recurring

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_event(fixture_name: str) -> Component:
    """Parse the first ``VEVENT`` out of a fixture file under ``tests/fixtures/``."""
    data = (_FIXTURES_DIR / fixture_name).read_bytes()
    calendar = Calendar.from_ical(data)
    events: list[Component] = calendar.walk("VEVENT")
    return events[0]


def test_event_ending_entirely_before_date_is_deleted() -> None:
    """A timed event fully in the past relative to DATE is deleted."""
    event = _load_event("non_recurring_ends_before.ics")

    action = classify_non_recurring(event, date(2024, 3, 15), UTC)

    assert action == Action.DELETE


def test_event_straddling_date_is_skipped() -> None:
    """An event that starts before DATE and is still ongoing at/after it is skipped."""
    event = _load_event("non_recurring_straddles.ics")

    action = classify_non_recurring(event, date(2024, 3, 15), UTC)

    assert action == Action.SKIP_STRADDLING


def test_event_starting_on_or_after_date_is_untouched() -> None:
    """An event that starts on or after DATE is left alone."""
    event = _load_event("non_recurring_starts_after.ics")

    action = classify_non_recurring(event, date(2024, 3, 15), UTC)

    assert action == Action.UNTOUCHED


def test_event_starting_exactly_on_date_is_untouched() -> None:
    """The boundary itself belongs to the untouched side, not the straddling one."""
    event = _load_event("non_recurring_starts_after.ics")

    action = classify_non_recurring(event, date(2024, 3, 20), UTC)

    assert action == Action.UNTOUCHED


def test_all_day_event_with_exclusive_dtend_on_boundary_is_deleted() -> None:
    """An off-by-one trap: naive inclusive DTEND handling would call this straddling.

    The fixture's single-day all-day event (DTSTART 2024-03-14, DTEND
    2024-03-15) occupies only March 14th, since DTEND is exclusive per brief
    §5.1. Against a DATE boundary of 2024-03-15, the whole event is already
    over, so it must be deleted, not skipped as straddling.
    """
    event = _load_event("non_recurring_all_day_boundary.ics")

    action = classify_non_recurring(event, date(2024, 3, 15), UTC)

    assert action == Action.DELETE


def test_multi_day_all_day_event_straddling_boundary_is_skipped() -> None:
    """A multi-day all-day event whose DATE range still contains the boundary straddles.

    Unlike the single-day fixture above, this event's DTSTART (13th) is
    strictly before DATE and its exclusive DTEND (16th) is strictly after
    it, so the boundary falls on a day the event actually occupies (14th or
    15th) — the genuine straddling case, distinct from the exclusive-DTEND
    off-by-one trap.
    """
    event = _load_event("non_recurring_all_day_straddles.ics")

    action = classify_non_recurring(event, date(2024, 3, 15), UTC)

    assert action == Action.SKIP_STRADDLING


def test_floating_datetime_is_resolved_in_the_run_timezone() -> None:
    """A floating DTSTART/DTEND is interpreted in the resolved zone before comparison."""
    event = _load_event("non_recurring_floating_straddles.ics")

    action = classify_non_recurring(event, date(2024, 3, 15), ZoneInfo("Europe/Rome"))

    assert action == Action.SKIP_STRADDLING
