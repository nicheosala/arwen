"""Recurrence pruning engine.

Pure functions that take ``icalendar`` objects and return ``icalendar``
objects. No I/O happens in this module.

Only :func:`expand_occurrences` touches ``recurring-ical-events``, and only
for occurrence expansion — never to make a pruning or mutation decision. That
division of labour is a CLAUDE.md §1.1 / brief §5.4 invariant: everything
else in this module (still to be added) is hand-written.
"""

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import recurring_ical_events

from arwen.model import AllDayDate, EventTime, Occurrence, parse_event_time

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
