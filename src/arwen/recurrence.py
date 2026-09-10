"""Recurrence pruning engine.

Pure functions that take ``icalendar`` objects and return ``icalendar``
objects. No I/O happens in this module, and nothing here reads the clock:
:func:`prune_recurring` takes the ``DTSTAMP`` it should stamp as an argument.

Only :func:`expand_occurrences` touches ``recurring-ical-events``, and only
for occurrence expansion — never to make a pruning or mutation decision. That
division of labour is a CLAUDE.md §1.1 / brief §5.4 invariant: every mutation
below is hand-written. The one other third-party call is ``dateutil.rrule``,
used to enumerate an ``RRULE``'s instants — recurrence arithmetic, not a
pruning decision — most importantly for the ``COUNT`` to ``UNTIL`` conversion
that must be computed from the *original* ``DTSTART``.
"""

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING

import recurring_ical_events
from dateutil.rrule import rrulestr
from icalendar import Calendar
from icalendar.prop import vDDDLists, vDDDTypes, vInt, vRecur

from arwen.model import (
    Action,
    AllDayDate,
    EventTime,
    FloatingDateTime,
    Instant,
    Occurrence,
    PruneResult,
    PruneStrategy,
    parse_event_time,
)

if TYPE_CHECKING:
    from icalendar import Component

VALIDATION_WINDOW_YEARS: int = 10
"""How far past ``DATE`` an unbounded series is compared, per brief §5.4 step 6.

An unbounded ``RRULE`` has no last occurrence, so the validation gate of step 4
cannot compare the two expansions exhaustively. It compares them over a finite
window instead, running from the earliest component start to ``DATE`` plus this
many years. A series whose surviving occurrences all fall beyond the window is
reported as unprunable rather than pruned on incomplete evidence.
"""

_MAX_RULE_INSTANTS: int = 50_000
"""Ceiling on the instants a single ``RRULE`` may contribute to one expansion.

A sub-daily ``FREQ`` over the validation window can generate millions of
instants. Rather than stall, the resource is reported as unprunable.
"""


class _UnprunableError(Exception):
    """Raised internally when a resource cannot be pruned safely.

    Caught by :func:`prune_recurring`, which turns it into
    :attr:`~arwen.model.Action.SKIP_UNPRUNABLE`. Never escapes this module.
    """


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


def is_recurring(calendar: Calendar) -> bool:
    """Report whether a resource is recurring, per brief §5.4.

    A resource counts as recurring if **any** of its event components carries
    ``RRULE``, ``RDATE``, or ``RECURRENCE-ID`` — not merely the first one, so
    a resource that is nothing but overrides is still routed to the pruning
    engine. Only ``VEVENT`` components are inspected: the ``STANDARD`` and
    ``DAYLIGHT`` subcomponents of a ``VTIMEZONE`` carry ``RRULE`` of their own
    and say nothing about whether the event recurs.
    """
    return any(
        component.get(name) is not None
        for component in calendar.walk("VEVENT")
        for name in ("RRULE", "RDATE", "RECURRENCE-ID")
    )


def _copy_calendar(calendar: Calendar) -> Calendar:
    """Return an independent deep copy of ``calendar``, via a serialization round-trip.

    Going through ``to_ical``/``from_ical`` copies unknown and ``X-``
    properties verbatim (brief §5.4 step 5) without this module needing to
    know what they are, and produces exactly the byte shape a later ``PUT``
    would send.
    """
    copied = Calendar.from_ical(calendar.to_ical().decode("utf-8"))
    if not isinstance(copied, Calendar):
        raise _UnprunableError("resource does not parse back as a single VCALENDAR")
    return copied


def _components_by_role(calendar: Calendar) -> tuple[Component | None, list[Component]]:
    """Split the event components into the master and its ``RECURRENCE-ID`` overrides.

    Returns:
        The master component — the first ``VEVENT`` without a
        ``RECURRENCE-ID`` — or ``None`` if the resource has only overrides
        (the orphan-override case), together with every override component in
        document order.
    """
    master: Component | None = None
    overrides: list[Component] = []
    for component in calendar.walk("VEVENT"):
        if component.get("RECURRENCE-ID") is None:
            if master is None:
                master = component
        else:
            overrides.append(component)
    return master, overrides


def _instant_key(value: date | datetime, zone: tzinfo) -> datetime:
    """Return a hashable, totally ordered key for a recurrence instant.

    Instants within one resource share ``DTSTART``'s flavour, but a
    ``RDATE``, ``EXDATE``, or ``RECURRENCE-ID`` may carry a different ``TZID``
    while denoting the same moment; RFC 5545 matches those by instant, so
    every aware value is normalized to UTC. Floating values are anchored in
    the run's resolved zone (brief §5.1) and all-day dates at UTC midnight,
    which keeps the order total even across a malformed mixture of flavours.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=zone).astimezone(UTC)
        return value.astimezone(UTC)
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _event_time_key(value: EventTime, zone: tzinfo) -> datetime:
    """Return the :func:`_instant_key` of an already-classified :data:`EventTime`."""
    if isinstance(value, Instant):
        return value.value
    if isinstance(value, FloatingDateTime):
        return value.value.replace(tzinfo=zone).astimezone(UTC)
    return datetime(value.value.year, value.value.month, value.value.day, tzinfo=UTC)


def _ends_before_boundary(end: date | datetime, boundary: date, zone: tzinfo) -> bool:
    """Report whether an occurrence ending at ``end`` is entirely before ``DATE``.

    Ends are exclusive (RFC 5545 §3.6.1, and brief §5.1 for all-day values),
    so an occurrence whose end falls exactly on the boundary is already over.
    """
    return _compare_to_boundary(parse_event_time(end), boundary, zone) <= 0


def _add_years(value: date, years: int) -> date:
    """Return ``value`` advanced by whole ``years``, clamping 29 February to the 28th."""
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def _recur_properties(component: Component) -> list[vRecur]:
    """Return every ``RRULE`` of a component.

    RFC 5545 §3.8.5.3 deprecates more than one ``RRULE`` per component but
    does not forbid it, and ``icalendar`` surfaces the repeated form as a
    list, so both shapes are normalized here.
    """
    prop = component.get("RRULE")
    if prop is None:
        return []
    if isinstance(prop, vRecur):
        return [prop]
    if isinstance(prop, list):
        return [rule for rule in prop if isinstance(rule, vRecur)]
    raise _UnprunableError("RRULE has an unsupported representation")


def _rule_text(rule: vRecur) -> str:
    """Render an ``RRULE`` back to its RFC 5545 value form, ready for ``dateutil``."""
    return rule.to_ical().decode("utf-8")


def _date_list_properties(component: Component, name: str) -> list[vDDDLists]:
    """Return every ``RDATE``/``EXDATE`` property of a component, repeated or not.

    The properties themselves are returned rather than their values, so that
    the ``TZID`` and ``VALUE`` parameters of each can be preserved when the
    list is filtered and written back.
    """
    prop = component.get(name)
    if prop is None:
        return []
    if isinstance(prop, vDDDLists):
        return [prop]
    if isinstance(prop, list):
        return [item for item in prop if isinstance(item, vDDDLists)]
    raise _UnprunableError(f"{name} has an unsupported representation")


def _date_list_values(prop: vDDDLists) -> list[date | datetime]:
    """Return the date/date-time values of one ``RDATE``/``EXDATE`` property.

    Raises:
        _UnprunableError: If the property carries a ``PERIOD`` value, which
            this engine does not know how to shift or exclude safely.
    """
    values: list[date | datetime] = []
    for item in prop.dts:
        value = item.dt
        if not isinstance(value, (date, datetime)):
            raise _UnprunableError("RDATE/EXDATE PERIOD values are not supported")
        values.append(value)
    return values


def _all_date_list_values(component: Component, name: str) -> list[date | datetime]:
    """Return every ``RDATE``/``EXDATE`` value of a component, flattened."""
    return [
        value
        for prop in _date_list_properties(component, name)
        for value in _date_list_values(prop)
    ]


def _nominal_duration(component: Component, start: date | datetime) -> timedelta:
    """Return the span the master's ``DTSTART`` covers, per brief §5.2.

    This is the span each rule-generated occurrence inherits, and the span the
    ``DTSTART`` shift must preserve.
    """
    end = _read_date_or_datetime(component, "DTEND")
    if end is not None:
        if isinstance(start, datetime):
            if not isinstance(end, datetime):
                raise _UnprunableError("DTSTART and DTEND have different value types")
            return end - start
        if isinstance(end, datetime):
            raise _UnprunableError("DTSTART and DTEND have different value types")
        return end - start
    duration = _read_duration(component)
    if duration is not None:
        return duration
    return timedelta(0) if isinstance(start, datetime) else timedelta(days=1)


def _horizon_for(start: date | datetime, horizon: date) -> date | datetime:
    """Express the validation horizon in the same flavour as ``start``."""
    if isinstance(start, datetime):
        return datetime.combine(horizon, time.min, tzinfo=start.tzinfo)
    return horizon


def _rule_instants(
    component: Component, start: date | datetime, horizon: date
) -> list[date | datetime]:
    """Enumerate the instants every ``RRULE`` of ``component`` generates up to ``horizon``.

    ``dateutil.rrule`` does the recurrence arithmetic; the instants come back
    in ``DTSTART``'s own flavour, so an all-day series yields dates and a
    ``TZID``-qualified one yields date-times in that same zone.
    """
    rules = _recur_properties(component)
    if not rules:
        return []

    all_day = not isinstance(start, datetime)
    first = datetime.combine(start, time.min) if all_day else start
    last = _horizon_for(first, horizon)

    instants: list[date | datetime] = []
    for rule in rules:
        stream = rrulestr(_rule_text(rule), dtstart=first).xafter(first, inc=True)
        for generated, value in enumerate(stream, start=1):
            if value > last:
                break
            if generated > _MAX_RULE_INSTANTS:
                raise _UnprunableError("RRULE generates too many instants to prune safely")
            instants.append(value.date() if all_day else value)
    return instants


def _until_value(last: datetime, start: date | datetime) -> date | datetime:
    """Render a computed last occurrence as an ``UNTIL`` value matching ``DTSTART``.

    RFC 5545 §3.3.10 ties ``UNTIL``'s value type to ``DTSTART``'s: a date for
    an all-day series, a floating date-time for a floating one, and UTC for
    anything zone-qualified.
    """
    if not isinstance(start, datetime):
        return last.date()
    if start.tzinfo is None:
        return last.replace(tzinfo=None)
    return last.astimezone(UTC)


def _convert_count_to_until(component: Component, start: date | datetime) -> None:
    """Rewrite every ``COUNT``-bounded ``RRULE`` as an ``UNTIL``-bounded one in place.

    ``COUNT`` is relative to ``DTSTART`` (RFC 5545 §3.3.10), so shifting
    ``DTSTART`` forward without this conversion would invent as many future
    occurrences as were pruned from the past — the single most dangerous bug
    in this command (brief §5.4 step 2). The last occurrence is therefore
    computed from the **original** ``DTSTART``, which is what ``start`` must
    be, before any shift is applied.
    """
    all_day = not isinstance(start, datetime)
    first = datetime.combine(start, time.min) if all_day else start

    for rule in _recur_properties(component):
        counts = rule.get("COUNT")
        if not counts:
            continue
        count = int(next(iter(counts)))
        last: datetime | None = None
        for index, value in enumerate(rrulestr(_rule_text(rule), dtstart=first)):
            last = value
            if index + 1 >= count:
                break
        if last is None:
            raise _UnprunableError("RRULE with COUNT generates no occurrences")
        del rule["COUNT"]
        rule["UNTIL"] = [_until_value(last, start)]


def _set_date_property(component: Component, name: str, value: date | datetime) -> None:
    """Replace a date/date-time property in place, keeping its unrelated parameters.

    ``VALUE`` and ``TZID`` are re-derived from ``value`` itself, so an all-day
    property stays ``VALUE=DATE`` and a zone-qualified one keeps its ``TZID``;
    any other parameter on the original property is carried over untouched.
    """
    existing: vDDDTypes | None = component.get(name)
    prop = vDDDTypes(value)
    if existing is not None:
        for parameter, parameter_value in existing.params.items():
            if parameter.upper() not in ("TZID", "VALUE"):
                prop.params[parameter] = parameter_value
    component[name] = prop


def _make_date_list(values: list[date | datetime]) -> vDDDLists:
    """Build one ``RDATE``/``EXDATE`` property from same-flavour ``values``."""
    prop = vDDDLists(values)
    if not isinstance(values[0], datetime):
        prop.params["VALUE"] = "DATE"
    return prop


def _filter_date_list(component: Component, name: str, keep: set[datetime], zone: tzinfo) -> None:
    """Drop every ``RDATE``/``EXDATE`` value whose instant key is not in ``keep``.

    Properties are filtered in place rather than rebuilt from scratch, so each
    surviving value keeps the ``TZID`` it was written with.
    """
    kept: list[vDDDLists] = []
    for prop in _date_list_properties(component, name):
        values = [v for v in _date_list_values(prop) if _instant_key(v, zone) in keep]
        if values:
            replacement = _make_date_list(values)
            replacement.params = prop.params
            kept.append(replacement)
    if name in component:
        del component[name]
    if kept:
        component[name] = kept if len(kept) > 1 else kept[0]


def _add_exdates(component: Component, values: list[date | datetime], zone: tzinfo) -> None:
    """Append one ``EXDATE`` per instant in ``values``, grouped by zone.

    A single ``EXDATE`` property carries one ``TZID`` for all of its values,
    so values are grouped by their own zone before being written.
    """
    if not values:
        return

    def sort_key(value: date | datetime) -> datetime:
        return _instant_key(value, zone)

    groups: dict[str, list[date | datetime]] = {}
    for value in sorted(values, key=sort_key):
        tag = str(value.tzinfo) if isinstance(value, datetime) and value.tzinfo else ""
        groups.setdefault(tag, []).append(value)

    properties = _date_list_properties(component, "EXDATE")
    properties.extend(_make_date_list(group) for group in groups.values())
    if "EXDATE" in component:
        del component["EXDATE"]
    component["EXDATE"] = properties if len(properties) > 1 else properties[0]


def _bump_revision(component: Component, now: datetime) -> None:
    """Bump ``SEQUENCE`` and refresh ``DTSTAMP``/``LAST-MODIFIED``, per brief §5.4 step 5."""
    sequence = component.get("SEQUENCE")
    component["SEQUENCE"] = vInt(int(sequence) + 1 if sequence is not None else 1)
    stamp = vDDDTypes(now.astimezone(UTC))
    component["DTSTAMP"] = stamp
    component["LAST-MODIFIED"] = vDDDTypes(now.astimezone(UTC))


def _recurrence_instants(
    master: Component, start: date | datetime, horizon: date, zone: tzinfo
) -> list[date | datetime]:
    """Compute the master's recurrence set up to ``horizon``, in chronological order.

    The set is ``DTSTART`` together with every ``RRULE``-generated instant and
    every ``RDATE``, minus every ``EXDATE`` (RFC 5545 §3.8.5). ``DTSTART`` is
    included explicitly because it belongs to the recurrence set even when a
    resource carries ``RDATE`` alone, or an ``RRULE`` whose ``BY`` parts do
    not regenerate it. Values denoting the same instant collapse to one entry.
    """
    excluded = {_instant_key(value, zone) for value in _all_date_list_values(master, "EXDATE")}
    collected: dict[datetime, date | datetime] = {}
    candidates: list[date | datetime] = [
        start,
        *_rule_instants(master, start, horizon),
        *_all_date_list_values(master, "RDATE"),
    ]
    for value in candidates:
        key = _instant_key(value, zone)
        if key not in excluded:
            collected.setdefault(key, value)
    return [collected[key] for key in sorted(collected)]


def _overrides_by_instant(overrides: list[Component], zone: tzinfo) -> dict[datetime, Component]:
    """Index override components by the instant their ``RECURRENCE-ID`` replaces."""
    indexed: dict[datetime, Component] = {}
    for override in overrides:
        recurrence_id = _read_date_or_datetime(override, "RECURRENCE-ID")
        if recurrence_id is not None:
            indexed[_instant_key(recurrence_id, zone)] = override
    return indexed


def _override_survives(override: Component, boundary: date, zone: tzinfo) -> bool:
    """Report whether an override component's own instance reaches ``DATE`` or beyond."""
    return _compare_to_boundary(effective_end(override), boundary, zone) > 0


def _instant_survives(
    instant: date | datetime,
    duration: timedelta,
    overrides: dict[datetime, Component],
    boundary: date,
    zone: tzinfo,
) -> bool:
    """Report whether the occurrence at ``instant`` reaches ``DATE`` or beyond.

    An overridden instant is judged by the override's own times, since that is
    when the occurrence actually happens; every other instant inherits the
    master's nominal duration.
    """
    override = overrides.get(_instant_key(instant, zone))
    if override is not None:
        return _override_survives(override, boundary, zone)
    return not _ends_before_boundary(instant + duration, boundary, zone)


def _drop_dead_overrides(calendar: Calendar, boundary: date, zone: tzinfo) -> None:
    """Remove every ``RECURRENCE-ID`` override whose instance is entirely before ``DATE``.

    Brief §5.4 step 2 removes such overrides as whole components — the
    occurrence is gone, so the exception that described it has nothing left to
    describe.
    """
    doomed = {
        id(component)
        for component in calendar.walk("VEVENT")
        if component.get("RECURRENCE-ID") is not None
        and not _override_survives(component, boundary, zone)
    }
    if doomed:
        calendar.subcomponents = [
            component for component in calendar.subcomponents if id(component) not in doomed
        ]


def _master_context(
    candidate: Calendar, zone: tzinfo, horizon: date
) -> tuple[Component, date | datetime, timedelta, list[date | datetime], dict[datetime, Component]]:
    """Gather everything both pruning strategies need about a candidate's master.

    Returns:
        The master component, its ``DTSTART``, the nominal duration each
        rule-generated occurrence inherits, the master's recurrence set up to
        ``horizon``, and its overrides indexed by the instant they replace.

    Raises:
        _UnprunableError: If the candidate has no master component or its
            master has no ``DTSTART``.
    """
    master, overrides = _components_by_role(candidate)
    if master is None:
        raise _UnprunableError("resource has no master component")
    start = _read_date_or_datetime(master, "DTSTART")
    if start is None:
        raise _UnprunableError("master component has no DTSTART")
    return (
        master,
        start,
        _nominal_duration(master, start),
        _recurrence_instants(master, start, horizon, zone),
        _overrides_by_instant(overrides, zone),
    )


def _build_shift_candidate(
    calendar: Calendar, boundary: date, zone: tzinfo, horizon: date, now: datetime
) -> Calendar | None:
    """Build the primary, ``DTSTART``-shifted pruning of a resource (brief §5.4 step 2).

    ``COUNT`` is converted to ``UNTIL`` from the original ``DTSTART`` first,
    then ``DTSTART`` moves to the first surviving instant with the event's
    duration preserved, pre-``DTSTART`` ``RDATE`` and ``EXDATE`` values are
    dropped as unreachable, and dead overrides are removed.

    Returns:
        The pruned calendar, or ``None`` if the resource has no master
        component, or if no instant of the master's own recurrence set
        survives and there is therefore nothing to shift to.
    """
    candidate = _copy_calendar(calendar)
    if _components_by_role(candidate)[0] is None:
        return None
    master, start, duration, instants, overrides = _master_context(candidate, zone, horizon)

    # ``start`` is still the original DTSTART here, which is what COUNT is
    # relative to; the conversion has to happen before the shift below.
    _convert_count_to_until(master, start)

    surviving = [
        instant
        for instant in instants
        if _instant_survives(instant, duration, overrides, boundary, zone)
    ]
    if not surviving:
        return None

    target = surviving[0]
    target_key = _instant_key(target, zone)
    _set_date_property(master, "DTSTART", target)
    if master.get("DTEND") is not None:
        _set_date_property(master, "DTEND", target + duration)

    _filter_date_list(master, "RDATE", {_instant_key(i, zone) for i in surviving}, zone)
    _filter_date_list(
        master,
        "EXDATE",
        {
            key
            for key in (_instant_key(v, zone) for v in _all_date_list_values(master, "EXDATE"))
            if key >= target_key
        },
        zone,
    )
    _drop_dead_overrides(candidate, boundary, zone)
    _bump_revision(master, now)
    return candidate


def _build_exdate_candidate(
    calendar: Calendar, boundary: date, zone: tzinfo, horizon: date, now: datetime
) -> Calendar:
    """Build the ``EXDATE``-only fallback pruning of a resource (brief §5.4 step 4).

    ``DTSTART`` and the ``RRULE`` are left exactly as they were; one ``EXDATE``
    is added per removed rule-generated instant, removed ``RDATE`` values are
    dropped instead — an ``RDATE`` that is deleted needs no exclusion — and
    dead overrides are removed as in the shift strategy.

    A resource made only of overrides has no master to exclude anything from,
    so for it this reduces to removing the dead overrides.
    """
    candidate = _copy_calendar(calendar)
    if _components_by_role(candidate)[0] is None:
        _drop_dead_overrides(candidate, boundary, zone)
        for survivor in _components_by_role(candidate)[1]:
            _bump_revision(survivor, now)
        return candidate

    master, start, duration, instants, overrides = _master_context(candidate, zone, horizon)
    survives = [
        _instant_survives(instant, duration, overrides, boundary, zone) for instant in instants
    ]
    rule_keys = {_instant_key(start, zone)} | {
        _instant_key(value, zone) for value in _rule_instants(master, start, horizon)
    }

    _filter_date_list(
        master,
        "RDATE",
        {_instant_key(i, zone) for i, kept in zip(instants, survives, strict=True) if kept},
        zone,
    )
    _add_exdates(
        master,
        [
            instant
            for instant, kept in zip(instants, survives, strict=True)
            if not kept and _instant_key(instant, zone) in rule_keys
        ],
        zone,
    )
    _drop_dead_overrides(candidate, boundary, zone)
    _bump_revision(master, now)
    return candidate


def _signature(occurrence: Occurrence, zone: tzinfo) -> tuple[datetime, datetime, str, str, str]:
    """Reduce an occurrence to the identity the validation gate compares.

    Start and end instants pin down when the occurrence happens; ``SUMMARY``,
    ``DESCRIPTION``, and ``LOCATION`` distinguish an override from the plain
    occurrence it replaces, so silently losing an override cannot pass as an
    unchanged expansion.
    """
    component = occurrence.component
    return (
        _event_time_key(occurrence.start, zone),
        _event_time_key(occurrence.end, zone),
        str(component.get("SUMMARY", "")),
        str(component.get("DESCRIPTION", "")),
        str(component.get("LOCATION", "")),
    )


def _expansion_window(
    calendar: Calendar, boundary: date, zone: tzinfo
) -> tuple[datetime, datetime]:
    """Choose the window over which the original and the pruned result are compared.

    It runs from just before the earliest component start — so that no
    occurrence eligible for removal is missed — to ``DATE`` plus
    :data:`VALIDATION_WINDOW_YEARS`, which bounds otherwise endless unbounded
    series (brief §5.4 step 6).
    """
    starts: list[datetime] = []
    for component in calendar.walk("VEVENT"):
        for name in ("DTSTART", "RECURRENCE-ID"):
            value = _read_date_or_datetime(component, name)
            if value is not None:
                starts.append(_instant_key(value, zone))

    upper = _instant_key(
        datetime.combine(_add_years(boundary, VALIDATION_WINDOW_YEARS), time.min, tzinfo=zone),
        zone,
    )
    lower = min(starts) - timedelta(days=1) if starts else upper - timedelta(days=1)
    return lower, max(upper, lower + timedelta(days=1))


def _signatures_in_window(
    calendar: Calendar, window: tuple[datetime, datetime], zone: tzinfo
) -> list[tuple[datetime, datetime, str, str, str]]:
    """Expand ``calendar`` over ``window`` and reduce it to a sorted signature multiset."""
    return sorted(_signature(o, zone) for o in expand_occurrences(calendar, *window))


def _has_unbounded_rule(calendar: Calendar) -> bool:
    """Report whether any component carries an ``RRULE`` with neither ``COUNT`` nor ``UNTIL``."""
    return any(
        not rule.get("COUNT") and not rule.get("UNTIL")
        for component in calendar.walk("VEVENT")
        for rule in _recur_properties(component)
    )


def _prune(calendar: Calendar, boundary: date, zone: tzinfo, now: datetime) -> PruneResult:
    """Run the brief §5.4 pruning steps, letting :class:`_UnprunableError` escape."""
    horizon = _add_years(boundary, VALIDATION_WINDOW_YEARS)
    window = _expansion_window(calendar, boundary, zone)
    occurrences = expand_occurrences(calendar, *window)
    survivors = sorted(
        _signature(occurrence, zone)
        for occurrence in occurrences
        if _compare_to_boundary(occurrence.end, boundary, zone) > 0
    )
    removed = len(occurrences) - len(survivors)

    if removed == 0:
        return PruneResult(action=Action.UNTOUCHED, kept=len(survivors))
    if not survivors:
        if _has_unbounded_rule(calendar):
            raise _UnprunableError("unbounded series with no surviving occurrence in the window")
        return PruneResult(action=Action.DELETE, removed=removed)

    master, _ = _components_by_role(calendar)
    strategies: tuple[PruneStrategy, ...] = (
        (PruneStrategy.OVERRIDE_REMOVAL,)
        if master is None
        else (PruneStrategy.DTSTART_SHIFT, PruneStrategy.EXDATE_ONLY)
    )
    for strategy in strategies:
        try:
            candidate = (
                _build_shift_candidate(calendar, boundary, zone, horizon, now)
                if strategy is PruneStrategy.DTSTART_SHIFT
                else _build_exdate_candidate(calendar, boundary, zone, horizon, now)
            )
        except _UnprunableError:
            continue
        if candidate is None:
            continue
        if _signatures_in_window(candidate, window, zone) == survivors:
            return PruneResult(
                action=Action.MODIFY,
                calendar=candidate,
                strategy=strategy,
                removed=removed,
                kept=len(survivors),
            )

    return PruneResult(action=Action.SKIP_UNPRUNABLE, removed=removed, kept=len(survivors))


def prune_recurring(
    calendar: Calendar, boundary: date, zone: tzinfo, *, now: datetime
) -> PruneResult:
    """Prune the occurrences of a recurring resource that lie entirely before ``DATE``.

    Implements brief §5.4 end to end. Occurrences are expanded once; if none
    is affected the resource is left untouched, and if none survives it is
    deleted outright. Otherwise the resource is pruned with the ``DTSTART``
    shift, falling back to ``EXDATE``-only pruning, and every candidate must
    reproduce exactly the surviving occurrences of the original before it is
    returned — an unvalidated result is never handed back.

    An occurrence that straddles ``DATE`` survives whole (brief §5.4 step 3):
    it is never trimmed by synthesising a ``RECURRENCE-ID`` override, which is
    the deliberate asymmetry with §5.3.

    Arguments:
        calendar: The parsed resource. It is never modified; a pruned result
            is always a copy.
        boundary: The ``DATE`` argument of ``delete before``.
        zone: The timezone resolved for this run (brief §5.1).
        now: The instant to stamp into ``DTSTAMP``/``LAST-MODIFIED``. Passed
            in rather than read from the clock, so this module stays pure.

    Returns:
        A :class:`~arwen.model.PruneResult` whose action is ``UNTOUCHED``,
        ``DELETE``, ``MODIFY`` (carrying the validated calendar to ``PUT``),
        or ``SKIP_UNPRUNABLE`` when no strategy passed the validation gate.
    """
    try:
        return _prune(calendar, boundary, zone, now)
    except _UnprunableError, ValueError, TypeError:
        return PruneResult(action=Action.SKIP_UNPRUNABLE)
