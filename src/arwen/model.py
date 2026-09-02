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
from enum import Enum
from typing import TYPE_CHECKING, NewType

if TYPE_CHECKING:
    from icalendar import Calendar, Component


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


class Action(Enum):
    """The per-resource action taken, or that would be taken, by a delete run.

    Recorded in the report (brief §8) for every resource that classification,
    pruning, or de-duplication decided to act on. Kept as an enum rather than
    a bare string so a new outcome cannot be introduced by typo (CLAUDE.md
    §1.1).
    """

    DELETE = "delete"
    MODIFY = "modify"
    SKIP_STRADDLING = "skip-straddling"
    SKIP_UNPRUNABLE = "skip-unprunable"
    UNTOUCHED = "untouched"
    KEEP = "keep"
    NEEDS_REVIEW = "needs-review"


class PruneStrategy(Enum):
    """How :func:`arwen.recurrence.prune_recurring` reached its result.

    Brief §5.4 makes the ``DTSTART`` shift the primary strategy and
    ``EXDATE``-only pruning the fallback taken when the shifted result fails
    the validation gate. A resource made only of ``RECURRENCE-ID`` overrides
    has no master ``DTSTART`` to shift, so dropping the dead overrides is a
    strategy of its own rather than a degenerate shift.
    """

    DTSTART_SHIFT = "dtstart-shift"
    EXDATE_ONLY = "exdate-only"
    OVERRIDE_REMOVAL = "override-removal"


@dataclass(frozen=True, slots=True)
class PruneResult:
    """The outcome of pruning one recurring resource, per brief §5.4.

    Attributes:
        action: What the caller should do with the resource. ``MODIFY`` is
            the only action that carries a :attr:`calendar`.
        calendar: The validated, pruned calendar to ``PUT`` back, or ``None``
            when nothing is to be written. Never an unvalidated result
            (brief §5.4 step 4).
        strategy: Which strategy produced :attr:`calendar`, or ``None`` when
            no pruning happened.
        removed: How many occurrences of the original were dropped.
        kept: How many occurrences of the original survive.
    """

    action: Action
    calendar: Calendar | None = None
    strategy: PruneStrategy | None = None
    removed: int = 0
    kept: int = 0


ContentHash = NewType("ContentHash", str)
"""A SHA-256 hex digest of a resource's canonicalized, volatile-property-free content.

Produced by :func:`arwen.dedup.content_hash`. A distinct type from a bare
``str`` so a UID or an ETag can never be passed to a comparison expecting one
(CLAUDE.md §1.1).
"""


@dataclass(frozen=True, slots=True)
class DuplicateKey:
    """The brief §6.2 duplicate key: normalized summary, start, end, all-day flag.

    Two resources share a key exactly when :attr:`summary` matches
    case-sensitively and :attr:`start`/:attr:`end` denote the same instants
    (or the same pure dates, for an all-day event) — regardless of which
    ``TZID`` they were originally expressed in, since :class:`Instant`
    normalizes to UTC before comparison. :attr:`all_day` guarantees a timed
    and an all-day event with a coincidentally matching key never match, even
    though that already follows from :attr:`start` and :attr:`end` never
    comparing equal across an :class:`Instant`/:class:`AllDayDate` type
    mismatch — the brief lists it as an explicit field, so it is kept
    explicit here too.
    """

    summary: str
    start: EventTime
    end: EventTime
    all_day: bool


@dataclass(frozen=True, slots=True)
class DedupCandidate:
    """One resource considered for duplicate detection, per brief §6.

    Carries only the identity a caller needs to act on a grouping decision —
    ``href`` and ``etag``, for the ``DELETE``/``If-Match`` of brief §7 — plus
    the parsed resource itself. :mod:`arwen.dedup` never reads ``href`` or
    ``etag``: they are excluded from the content hash by construction, not by
    an entry in its exclusion list.
    """

    href: str
    etag: str
    calendar: Calendar


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    """The de-duplication outcome for one duplicate key, per brief §6.3.

    A key group can contain more than one content-identical sub-group (brief
    §6.3 step 4's "3 identical plus 2 divergent" shape), so :attr:`kept` holds
    one winner *per* content-identical sub-group of size ≥ 2, not one winner
    for the whole key. :attr:`needs_review` holds every member of a sub-group
    that could not be confidently deduplicated — a sub-group of exactly one,
    coexisting with at least one other distinct sub-group under the same key.
    A key group with no duplication at all (a single member, or every member
    distinct with nothing else to compare it against) produces no
    :class:`DuplicateGroup` — there is nothing to report.
    """

    key: DuplicateKey
    kept: tuple[DedupCandidate, ...]
    to_delete: tuple[DedupCandidate, ...]
    needs_review: tuple[DedupCandidate, ...]
