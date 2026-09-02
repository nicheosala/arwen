"""Duplicate detection: key derivation, content hashing, grouping, and tie-break.

Pure functions that take ``icalendar`` objects and return ``icalendar``
objects (or plain data derived from them). No I/O happens in this module.

Implements brief §6 end to end:

- :func:`duplicate_key` and :func:`normalize_summary` — the §6.2 key.
- :func:`content_hash` — the §6.3 step 2 canonical content hash.
- :func:`group_duplicates` — the §6.3 grouping algorithm and winner tie-break,
  applied by content-identical sub-group rather than pairwise, so five copies
  of one event reduce to exactly one survivor rather than two (§6.3's
  classic bug).
- :func:`is_examinable` — the §6.1 filter (recurring resources and resources
  with no ``VEVENT`` are never grouped, however their key compares).

Recurrence detection is delegated to :func:`arwen.recurrence.is_recurring`
rather than duplicated here, since it is the same brief §5.4 definition:
any component carrying ``RRULE``, ``RDATE``, or ``RECURRENCE-ID``.
"""

import hashlib
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from arwen.model import (
    AllDayDate,
    ContentHash,
    DedupCandidate,
    DuplicateGroup,
    DuplicateKey,
)
from arwen.recurrence import effective_end, is_recurring, start_of

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from icalendar import Calendar, Component

_EXCLUDED_PROPERTIES: frozenset[str] = frozenset(
    {"UID", "DTSTAMP", "LAST-MODIFIED", "CREATED", "SEQUENCE", "PRODID"}
)
"""Volatile properties excluded from the content hash, per brief §6.3 step 2.

Also documented in the README as the property exclusion list (brief §11).
``href`` and ``ETag`` need no entry here: :func:`content_hash` never sees
them, since it takes a parsed :class:`~icalendar.Calendar`, not a
:class:`~arwen.model.DedupCandidate`.
"""


@runtime_checkable
class _PropertyValue(Protocol):
    """Structural shape shared by every parsed ``icalendar`` property value.

    ``Component.property_items`` types its values as plain ``object``, since
    the concrete type varies by property (``vText``, ``vDDDTypes``,
    ``vCalAddress``, ...). This is the minimal shape :func:`_canonical_line`
    needs from any of them, checked structurally at runtime so no ``Any``
    from an unrecognised concrete type leaks into this module.
    """

    params: Mapping[str, object]

    def to_ical(self) -> bytes:
        """Render this property's value back to its RFC 5545 wire form."""
        ...


def is_examinable(calendar: Calendar) -> bool:
    """Report whether a resource is eligible for duplicate detection, per brief §6.1.

    A resource is skipped — and reported as "not examined" by the caller,
    never grouped by :func:`group_duplicates` — if it is recurring (any
    component carries ``RRULE``, ``RDATE``, or ``RECURRENCE-ID``) or if it
    has no ``VEVENT`` component at all (``VTODO``, ``VJOURNAL``). Recurring
    de-duplication is deferred to a later iteration (brief §12).
    """
    return bool(calendar.walk("VEVENT")) and not is_recurring(calendar)


def normalize_summary(summary: str) -> str:
    """Trim and collapse the internal whitespace of a ``SUMMARY``, per brief §6.2.

    Comparison stays case-sensitive: only whitespace is normalized. A missing
    ``SUMMARY`` normalizes to the empty string via the caller passing ``""``,
    which remains eligible for matching other empty-summary events.
    """
    return " ".join(summary.split())


def _primary_event(calendar: Calendar) -> Component:
    """Return the resource's sole examined ``VEVENT``.

    Raises:
        ValueError: If the resource has no ``VEVENT``. Callers are expected
            to have already filtered with :func:`is_examinable`.
    """
    events: list[Component] = calendar.walk("VEVENT")
    if not events:
        raise ValueError("resource has no VEVENT component")
    return events[0]


def duplicate_key(calendar: Calendar) -> DuplicateKey:
    """Derive the brief §6.2 duplicate key of a resource's ``VEVENT``.

    ``start`` and ``end`` are the same :data:`~arwen.model.EventTime` values
    :mod:`arwen.recurrence` uses for pruning — an :class:`~arwen.model.Instant`
    normalizes any ``TZID`` or UTC form to the same comparable value, so two
    ``DTSTART``s naming the same moment in different zones share a key. End is
    derived exactly as in brief §5.2, via :func:`arwen.recurrence.effective_end`.
    """
    event = _primary_event(calendar)
    start = start_of(event)
    end = effective_end(event)
    summary_property = event.get("SUMMARY")
    summary = normalize_summary(str(summary_property) if summary_property is not None else "")
    return DuplicateKey(
        summary=summary,
        start=start,
        end=end,
        all_day=isinstance(start, AllDayDate),
    )


def _canonical_line(name: str, value: object) -> str:
    """Render one property as a single canonical, order-normalized line.

    Parameters are sorted by name so two properties differing only in
    parameter order hash identically (brief §6.3 step 2). The ``BEGIN``/``END``
    markers ``property_items`` emits at each component boundary come through
    as plain ``bytes`` rather than a typed property value; they are rendered
    as-is so the hash still distinguishes structurally different resources
    (an extra ``VALARM``, for instance) even though it ignores property order.
    """
    if isinstance(value, bytes):
        return f"{name.upper()}\x1f\x1f{value.decode('utf-8')}"
    if not isinstance(value, _PropertyValue):
        raise TypeError(f"unsupported property value for {name}: {type(value)!r}")
    parameters = ";".join(
        f"{str(param_name).upper()}={param_value!s}"
        for param_name, param_value in sorted(value.params.items(), key=lambda item: str(item[0]))
    )
    text = value.to_ical().decode("utf-8")
    return f"{name.upper()}\x1f{parameters}\x1f{text}"


def content_hash(calendar: Calendar) -> ContentHash:
    """Compute the brief §6.3 step 2 canonical content hash of a resource.

    Every property of the resource — recursing into its subcomponents, so a
    ``VALARM`` is part of the identity a ``DESCRIPTION``, ``LOCATION``, or
    ``ATTENDEE`` also is — is rendered as a canonical line via
    :func:`_canonical_line`, with :data:`_EXCLUDED_PROPERTIES` dropped
    wherever they occur. The lines are then sorted, which normalizes property
    order (brief §6.3 step 2) and makes repeated properties (multiple
    ``ATTENDEE``s, say) order-independent too. The result is a SHA-256 hex
    digest of the sorted, newline-joined lines.
    """
    lines = sorted(
        _canonical_line(name, value)
        for name, value in calendar.property_items(recursive=True, sorted=True)
        if name.upper() not in _EXCLUDED_PROPERTIES
    )
    canonical = "\n".join(lines)
    return ContentHash(hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def _read_sequence(event: Component) -> int:
    """Return an event's ``SEQUENCE``, defaulting to 0 per RFC 5545 §3.8.7.4."""
    value = event.get("SEQUENCE")
    return int(value) if value is not None else 0


def _read_timestamp(event: Component, name: str) -> float:
    """Return the epoch-seconds timestamp of a date/date-time property.

    Returns negative infinity if the property is absent, so a candidate
    missing ``LAST-MODIFIED`` or ``DTSTAMP`` always loses that tie-break step
    to any candidate that has one, rather than crashing or comparing equal.
    """
    value = event.get(name)
    if value is None:
        return float("-inf")
    moment = value.dt
    if isinstance(moment, datetime):
        instant = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    else:
        instant = datetime.combine(moment, time.min, tzinfo=UTC)
    return instant.timestamp()


def _tie_break_key(candidate: DedupCandidate) -> tuple[int, float, float, str]:
    """Return the brief §6.3 tie-break as a key whose minimum is the winner.

    In priority order: highest ``SEQUENCE``, most recent ``LAST-MODIFIED``,
    most recent ``DTSTAMP``, lexicographically smallest ``UID``. The first
    three are negated so that "highest"/"most recent" sorts first under
    :func:`min`; ``UID`` already wants the smallest value first, so it is
    left alone. Depending only on each candidate's own resource data — never
    on its position in the input — is what makes the winner independent of
    input order (brief §6.3, proven in tests by shuffling the input).
    """
    event = _primary_event(candidate.calendar)
    uid = str(event.get("UID", ""))
    return (
        -_read_sequence(event),
        -_read_timestamp(event, "LAST-MODIFIED"),
        -_read_timestamp(event, "DTSTAMP"),
        uid,
    )


def _pick_winner(members: Sequence[DedupCandidate]) -> DedupCandidate:
    """Deterministically pick the tie-break winner of a content-identical sub-group."""
    return min(members, key=_tie_break_key)


def group_duplicates(candidates: Sequence[DedupCandidate]) -> list[DuplicateGroup]:
    """Group ``candidates`` by duplicate key and content hash, per brief §6.3.

    Never pairwise: every candidate sharing a :func:`duplicate_key` is
    collected first, then split into content-identical sub-groups by
    :func:`content_hash`. Within each sub-group of size ≥ 2, one winner is
    kept via :func:`_pick_winner` and the rest are marked for deletion — this
    is what makes five identical copies reduce to exactly one survivor rather
    than the classic pairwise bug that leaves two. A sub-group of exactly one
    is left untouched *unless* the key group also contains another, distinct
    sub-group, in which case it cannot be told apart from a genuine
    duplicate by key alone and is reported as needing manual review (brief
    §6.3 step 4) rather than silently ignored or guessed at.

    Non-:func:`examinable <is_examinable>` candidates — recurring resources,
    or resources with no ``VEVENT`` — are dropped before grouping starts, so
    a recurring resource can never be deduplicated or flagged merely because
    its key happens to coincide with an unrelated event's.

    A key shared by only one candidate produces no :class:`DuplicateGroup`:
    there is nothing to compare it against, so nothing to report.
    """
    examinable = [candidate for candidate in candidates if is_examinable(candidate.calendar)]

    by_key: dict[DuplicateKey, list[DedupCandidate]] = {}
    for candidate in examinable:
        by_key.setdefault(duplicate_key(candidate.calendar), []).append(candidate)

    groups: list[DuplicateGroup] = []
    for key, members in by_key.items():
        if len(members) < 2:
            continue

        by_hash: dict[ContentHash, list[DedupCandidate]] = {}
        for member in members:
            by_hash.setdefault(content_hash(member.calendar), []).append(member)

        kept: list[DedupCandidate] = []
        to_delete: list[DedupCandidate] = []
        needs_review: list[DedupCandidate] = []
        for sub_group in by_hash.values():
            if len(sub_group) >= 2:
                winner = _pick_winner(sub_group)
                kept.append(winner)
                to_delete.extend(member for member in sub_group if member is not winner)
            else:
                needs_review.extend(sub_group)

        groups.append(
            DuplicateGroup(
                key=key,
                kept=tuple(kept),
                to_delete=tuple(to_delete),
                needs_review=tuple(needs_review),
            )
        )

    return groups
