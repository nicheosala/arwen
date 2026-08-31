"""Domain types shared by the pruning and de-duplication engines.

RFC 5545 date-times come in three flavours that must never be confused: an
absolute instant (``TZID`` or a trailing ``Z``), a floating date-time (naive,
interpreted in whatever zone the run resolves), and an all-day date
(``VALUE=DATE``, compared with no time component at all). Brief §5.1 turns on
never mixing these up, so each flavour gets its own type here rather than a
bare :class:`datetime.datetime` that a caller could misinterpret.
"""

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from icalendar import Component


@dataclass(frozen=True, slots=True)
class Instant:
    """An unambiguous point in time, always normalized to UTC.

    Constructed from any timezone-aware :class:`datetime.datetime`, including
    ``DTSTART``/``DTEND`` values that carry a ``TZID`` or a trailing ``Z``.
    Two instants that denote the same moment compare equal regardless of
    which zone they were expressed in originally.
    """

    value: _dt.datetime

    def __post_init__(self) -> None:
        """Reject naive input and normalize the stored value to UTC."""
        if self.value.tzinfo is None:
            raise ValueError("Instant requires a timezone-aware datetime.")
        object.__setattr__(self, "value", self.value.astimezone(_dt.UTC))

    def __add__(self, delta: _dt.timedelta) -> Instant:
        """Return this instant shifted by ``delta``."""
        return Instant(self.value + delta)


@dataclass(frozen=True, slots=True)
class FloatingDateTime:
    """A floating date-time (no ``TZID``, no trailing ``Z``), per RFC 5545 §3.3.5.

    Carries no zone of its own; brief §5.1 requires it be interpreted in the
    run's resolved timezone, via :meth:`resolve`, before it can be compared
    against anything.
    """

    value: _dt.datetime

    def __post_init__(self) -> None:
        """Reject any input that carries a zone; a floating value must be naive."""
        if self.value.tzinfo is not None:
            raise ValueError("FloatingDateTime must be naive (no tzinfo).")

    def __add__(self, delta: _dt.timedelta) -> FloatingDateTime:
        """Return this floating date-time shifted by ``delta``."""
        return FloatingDateTime(self.value + delta)

    def resolve(self, zone: _dt.tzinfo) -> Instant:
        """Anchor this floating date-time in ``zone``, producing an Instant."""
        return Instant(self.value.replace(tzinfo=zone))


@dataclass(frozen=True, slots=True)
class AllDayDate:
    """An all-day date (``VALUE=DATE``), compared with no timezone conversion.

    Deliberately wraps a pure :class:`datetime.date`, not a
    :class:`datetime.datetime`, so an all-day value can never be compared
    against a timed one by accident.
    """

    value: _dt.date

    def __post_init__(self) -> None:
        """Reject anything that is not a pure date, including datetimes."""
        if type(self.value) is not _dt.date:
            raise ValueError("AllDayDate requires a pure date, not a datetime.")

    def __add__(self, delta: _dt.timedelta) -> AllDayDate:
        """Return this all-day date shifted by ``delta``."""
        return AllDayDate(self.value + delta)


EventTime = Instant | FloatingDateTime | AllDayDate
"""A DTSTART/DTEND value, classified into its RFC 5545 flavour."""


def parse_event_time(value: _dt.date | _dt.datetime) -> EventTime:
    """Classify a raw ``icalendar`` date/datetime value into its :data:`EventTime` flavour.

    Arguments:
        value: The ``.dt`` of a parsed ``DTSTART``/``DTEND`` property.

    Returns:
        An :class:`Instant` if ``value`` is timezone-aware, a
        :class:`FloatingDateTime` if it is a naive datetime, or an
        :class:`AllDayDate` if it is a pure date.
    """
    if isinstance(value, _dt.datetime):
        if value.tzinfo is not None:
            return Instant(value)
        return FloatingDateTime(value)
    return AllDayDate(value)


@dataclass(frozen=True, slots=True)
class Occurrence:
    """A single, concrete occurrence of an event component.

    Produced by :func:`arwen.recurrence.expand_occurrences`, the sole point
    of contact with ``recurring-ical-events`` (CLAUDE.md §1.1). ``start`` and
    ``end`` are pre-classified into :data:`EventTime` so later pruning logic
    never has to re-derive their timezone flavour.
    """

    uid: str
    start: EventTime
    end: EventTime
    component: Component
