"""Recurrence pruning engine.

Pure functions that take ``icalendar`` objects and return ``icalendar``
objects. No I/O happens in this module.

Only :func:`expand_occurrences` touches ``recurring-ical-events``, and only
for occurrence expansion — never to make a pruning or mutation decision. That
division of labour is a CLAUDE.md §1.1 / brief §5.4 invariant: everything
else in this module (still to be added) is hand-written.
"""

from datetime import date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING

import recurring_ical_events

from arwen.model import (
    Action,
    AllDayDate,
    EventTime,
    FloatingDateTime,
    Instant,
    Occurrence,
    parse_event_time,
)

if TYPE_CHECKING:
    from icalendar import Calendar, Component
    from icalendar.prop import vDDDTypes


def _read_date_or_datetime(component: Component, name: str) -> date | datetime | None:
    """Return the parsed ``.dt`` of a date/datetime property, or ``None`` if absent.

    Raises:
        ValueError: If the property is present but not a date or datetime
            (e.g. a ``DURATION`` or ``PERIOD`` value under this name).
    """
    prop: vDDDTypes | None = component.get(name)
    if prop is None:
        return None
    value = prop.dt
    if not isinstance(value, (date, datetime)):
        raise ValueError(f"{name} has an unsupported value type: {type(value)!r}")
    return value


def _read_duration(component: Component) -> timedelta | None:
    """Return the parsed ``DURATION`` of a component, or ``None`` if absent.

    Raises:
        ValueError: If ``DURATION`` is present but not a duration value.
    """
    prop: vDDDTypes | None = component.get("DURATION")
    if prop is None:
        return None
    value = prop.dt
    if not isinstance(value, timedelta):
        raise ValueError(f"DURATION has an unsupported value type: {type(value)!r}")
    return value


def start_of(component: Component) -> EventTime:
    """Return the classified ``DTSTART`` of an event component.

    Raises:
        ValueError: If the component has no ``DTSTART``.
    """
    value = _read_date_or_datetime(component, "DTSTART")
    if value is None:
        raise ValueError("component has no DTSTART")
    return parse_event_time(value)


def effective_end(component: Component) -> EventTime:
    """Derive the effective end of an event component, per brief §5.2.

    In order: ``DTEND``; else ``DTSTART`` + ``DURATION``; else, per RFC 5545,
    ``DTSTART`` for a timed event and ``DTSTART`` + one day for an all-day
    event. ``DTEND`` on an all-day event is exclusive already, so it is
    returned as-is — a day is added only when there is no ``DTEND`` at all.
    """
    start = start_of(component)

    dtend = _read_date_or_datetime(component, "DTEND")
    if dtend is not None:
        return parse_event_time(dtend)

    duration = _read_duration(component)
    if duration is not None:
        return start + duration

    if isinstance(start, AllDayDate):
        return start + timedelta(days=1)
    return start


def _compare_to_boundary(value: EventTime, boundary: date, zone: tzinfo) -> int:
    """Compare ``value`` against ``DATE`` at 00:00:00 in ``zone``, per brief §5.1.

    An :class:`AllDayDate` is compared as a pure date, with no zone
    conversion. A :class:`FloatingDateTime` is first resolved into ``zone``.
    An :class:`Instant` is compared directly, since it already carries an
    absolute moment.

    Returns:
        A negative number if ``value`` is strictly before the boundary, zero
        if it falls exactly on it, and a positive number if it is strictly
        after.
    """
    if isinstance(value, AllDayDate):
        if value.value < boundary:
            return -1
        if value.value > boundary:
            return 1
        return 0

    instant = value.resolve(zone) if isinstance(value, FloatingDateTime) else value
    boundary_instant = Instant(datetime(boundary.year, boundary.month, boundary.day, tzinfo=zone))
    if instant.value < boundary_instant.value:
        return -1
    if instant.value > boundary_instant.value:
        return 1
    return 0


def classify_non_recurring(component: Component, boundary: date, zone: tzinfo) -> Action:
    """Classify a non-recurring event against ``DATE``, per brief §5.3.

    ``effective_end`` is always exclusive (RFC 5545 §3.6.1, and — for
    all-day events — brief §5.1), so an event whose effective end falls
    exactly on the boundary has already finished before it and is deleted,
    not treated as straddling.

    Arguments:
        component: The event component to classify. Must be non-recurring;
            callers are responsible for routing recurring components (any
            ``RRULE``, ``RDATE``, or ``RECURRENCE-ID``) to the §5.4 pruning
            engine instead.
        boundary: The ``DATE`` argument of ``delete before``, as a pure
            calendar date.
        zone: The timezone resolved for this run (brief §5.1), used to
            interpret floating date-times and to anchor the boundary itself.

    Returns:
        :attr:`Action.DELETE` if the event ends entirely before ``DATE``,
        :attr:`Action.SKIP_STRADDLING` if it starts before ``DATE`` but is
        still ongoing at or after it, or :attr:`Action.UNTOUCHED` if it
        starts on or after ``DATE``.
    """
    start = start_of(component)
    end = effective_end(component)

    if _compare_to_boundary(end, boundary, zone) <= 0:
        return Action.DELETE
    if _compare_to_boundary(start, boundary, zone) < 0:
        return Action.SKIP_STRADDLING
    return Action.UNTOUCHED


def expand_occurrences(calendar: Calendar, start: datetime, end: datetime) -> list[Occurrence]:
    """Expand ``calendar`` into concrete occurrences between ``start`` and ``end``.

    This is the single, thin entry point into ``recurring-ical-events``
    (CLAUDE.md §1.1): the only place in the codebase where that library's
    untyped surface is touched. It never performs or decides a mutation — it
    only reports which occurrences exist, as fully typed :class:`Occurrence`
    values.
    """
    query = recurring_ical_events.of(calendar)
    raw_components: list[Component] = query.between(start, end)
    occurrences: list[Occurrence] = []
    for component in raw_components:
        occurrences.append(
            Occurrence(
                uid=str(component.get("UID", "")),
                start=start_of(component),
                end=effective_end(component),
                component=component,
            )
        )
    return occurrences
