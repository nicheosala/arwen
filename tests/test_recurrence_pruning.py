"""Unit tests for the brief §5.4 recurring-resource pruning engine.

Every pruning assertion below compares the expansion of the pruned resource
against an **explicitly written** list of instants (brief §10). The expander is
the tool under use, never the oracle: a test that compared one call to
``recurring-ical-events`` against another would prove nothing about the
hand-written pruning in :mod:`arwen.recurrence`.

Together with :mod:`tests.test_recurrence_classification`, this covers the
§10 pathological corpus: the ``COUNT`` conversion, the ``DTSTART`` shift,
``RDATE`` and ``EXDATE`` housekeeping, override removal, the revision bump,
the validation gate with its ``EXDATE``-only fallback, ``UNTIL``-bounded and
``BYSETPOS`` rules, a ``RECURRENCE-ID`` keyed by a UTC-equivalent instant of a
``TZID``-qualified master, and line folding, escaping, and non-ASCII text.
"""

from datetime import UTC, date, datetime, time, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from icalendar import Calendar

from arwen.model import (
    Action,
    AllDayDate,
    EventTime,
    FloatingDateTime,
    PruneResult,
    PruneStrategy,
)
from arwen.recurrence import expand_occurrences, is_recurring, prune_recurring

_FIXTURES_DIR = Path(__file__).parent / "fixtures"

_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
"""Fixed ``DTSTAMP``/``LAST-MODIFIED`` stamp, so the engine stays clock-free."""

_ROME = ZoneInfo("Europe/Rome")

_WINDOW_START = datetime(2014, 1, 1, tzinfo=UTC)
_WINDOW_END = datetime(2027, 1, 1, tzinfo=UTC)


def _load(fixture_name: str) -> Calendar:
    """Parse a fixture under ``tests/fixtures/`` into a :class:`icalendar.Calendar`."""
    parsed = Calendar.from_ical((_FIXTURES_DIR / fixture_name).read_text(encoding="utf-8-sig"))
    assert isinstance(parsed, Calendar)
    return parsed


def _render(value: EventTime) -> str:
    """Render an occurrence boundary compactly: ``YYYY-MM-DD`` or an ISO date-time."""
    if isinstance(value, AllDayDate):
        return value.value.isoformat()
    if isinstance(value, FloatingDateTime):
        return value.value.isoformat()
    return value.value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ends_before(value: EventTime, boundary: date, zone: tzinfo) -> bool:
    """Hand-written oracle for "this occurrence is entirely before ``DATE``".

    Deliberately independent of :mod:`arwen.recurrence`: the property checks
    below need something to compare the engine against that is not the engine.
    Ends are exclusive, so landing exactly on the boundary counts as over.
    """
    if isinstance(value, AllDayDate):
        return value.value <= boundary
    anchor = datetime.combine(boundary, time.min, tzinfo=zone)
    moment = (
        value.value.replace(tzinfo=zone) if isinstance(value, FloatingDateTime) else value.value
    )
    return moment <= anchor


def _starts(calendar: Calendar) -> list[str]:
    """Return the rendered start of every occurrence of ``calendar``, in order."""
    occurrences = expand_occurrences(calendar, _WINDOW_START, _WINDOW_END)
    return sorted(_render(occurrence.start) for occurrence in occurrences)


def _pruned(result: PruneResult) -> Calendar:
    """Assert that ``result`` modified the resource and return the pruned calendar."""
    assert result.action is Action.MODIFY
    assert result.calendar is not None
    return result.calendar


def _ical_text(calendar: Calendar) -> str:
    """Serialize a calendar and unfold its lines, so property values can be matched."""
    data: bytes = calendar.to_ical()
    return data.decode("utf-8").replace("\r\n ", "")


def _lines(calendar: Calendar) -> list[str]:
    """Return the unfolded content lines of a serialized calendar."""
    return _ical_text(calendar).splitlines()


def _prune_fixture(fixture_name: str, boundary: date, zone: tzinfo = UTC) -> PruneResult:
    """Prune a fixture at ``boundary`` in ``zone`` with the fixed test clock."""
    return prune_recurring(_load(fixture_name), boundary, zone, now=_NOW)


def test_recurring_detection_covers_every_component() -> None:
    """RRULE, RDATE, or RECURRENCE-ID on any VEVENT makes the resource recurring."""
    assert is_recurring(_load("recurring_count_weekly.ics"))
    assert is_recurring(_load("recurring_rdate_exdate.ics"))
    assert is_recurring(_load("recurring_orphan_overrides.ics"))
    assert not is_recurring(_load("non_recurring_ends_before.ics"))


def test_vtimezone_rrule_does_not_make_a_resource_recurring() -> None:
    """The RRULE inside a VTIMEZONE describes the zone, not the event."""
    assert not is_recurring(_load("recurring_in_vtimezone.ics"))


def test_count_is_converted_to_until_computed_from_the_original_dtstart() -> None:
    """The COUNT trap: shifting DTSTART without converting COUNT invents occurrences.

    The fixture runs weekly from 2024-01-01 with ``COUNT=10``, so its last
    occurrence is 2024-03-04. Pruning before 2024-02-01 drops the first five;
    a naive shift would leave ``COUNT=10`` in place and re-generate ten
    occurrences from 2024-02-05, inventing five that never existed.
    """
    result = _prune_fixture("recurring_count_weekly.ics", date(2024, 2, 1))
    pruned = _pruned(result)

    assert result.strategy is PruneStrategy.DTSTART_SHIFT
    assert (result.removed, result.kept) == (5, 5)
    assert "RRULE:FREQ=WEEKLY;UNTIL=20240304T090000Z" in _ical_text(pruned)
    assert not any(line.startswith("RRULE") and "COUNT" in line for line in _lines(pruned))
    assert _starts(pruned) == [
        "2024-02-05T09:00:00Z",
        "2024-02-12T09:00:00Z",
        "2024-02-19T09:00:00Z",
        "2024-02-26T09:00:00Z",
        "2024-03-04T09:00:00Z",
    ]


def test_dtstart_shift_preserves_the_event_duration() -> None:
    """DTEND moves with DTSTART, so every surviving occurrence keeps its span."""
    pruned = _pruned(_prune_fixture("recurring_count_weekly.ics", date(2024, 2, 1)))

    text = _ical_text(pruned)
    assert "DTSTART:20240205T090000Z" in text
    assert "DTEND:20240205T100000Z" in text

    ends = sorted(
        _render(occurrence.end)
        for occurrence in expand_occurrences(pruned, _WINDOW_START, _WINDOW_END)
    )
    assert ends == [
        "2024-02-05T10:00:00Z",
        "2024-02-12T10:00:00Z",
        "2024-02-19T10:00:00Z",
        "2024-02-26T10:00:00Z",
        "2024-03-04T10:00:00Z",
    ]


def test_duration_property_is_kept_as_is_when_dtstart_shifts() -> None:
    """A DURATION-based event needs no adjustment: only DTSTART moves."""
    pruned = _pruned(_prune_fixture("recurring_duration.ics", date(2024, 2, 1)))

    text = _ical_text(pruned)
    assert "DTSTART:20240205T090000Z" in text
    assert "DURATION:PT1H30M" in text
    assert not any(line.startswith("DTEND") for line in _lines(pruned))
    assert _starts(pruned)[0] == "2024-02-05T09:00:00Z"


def test_unknown_and_x_properties_survive_pruning_verbatim() -> None:
    """Brief §5.4 step 5: everything not named by the pruning rules is preserved."""
    pruned = _pruned(_prune_fixture("recurring_count_weekly.ics", date(2024, 2, 1)))

    assert "X-ARWEN-KEEPSAKE:preserve me verbatim" in _ical_text(pruned)


def test_sequence_is_bumped_and_stamps_are_refreshed() -> None:
    """Brief §5.4 step 5: a successful prune is a new revision of the resource."""
    pruned = _pruned(_prune_fixture("recurring_count_weekly.ics", date(2024, 2, 1)))

    text = _ical_text(pruned)
    assert "SEQUENCE:3" in text
    assert "DTSTAMP:20260901T120000Z" in text
    assert "LAST-MODIFIED:20260901T120000Z" in text


def test_all_day_series_keeps_value_date_and_its_one_day_span() -> None:
    """An all-day series prunes as pure dates, with the exclusive DTEND intact."""
    result = _prune_fixture("recurring_all_day_weekly.ics", date(2024, 2, 1))
    pruned = _pruned(result)

    text = _ical_text(pruned)
    assert "DTSTART;VALUE=DATE:20240205" in text
    assert "DTEND;VALUE=DATE:20240206" in text
    assert "RRULE:FREQ=WEEKLY;UNTIL=20240219" in text
    assert (result.removed, result.kept) == (5, 3)
    assert _starts(pruned) == ["2024-02-05", "2024-02-12", "2024-02-19"]


def test_rdate_before_the_new_dtstart_is_dropped_and_later_ones_are_kept() -> None:
    """An RDATE is independent of DTSTART, so a stale one would resurrect an occurrence."""
    result = _prune_fixture("recurring_rdate_exdate.ics", date(2024, 2, 1))
    pruned = _pruned(result)

    text = _ical_text(pruned)
    assert "RDATE:20240210T090000Z" in text
    assert "20240110T090000Z" not in text
    assert (result.removed, result.kept) == (5, 5)
    assert _starts(pruned) == [
        "2024-02-05T09:00:00Z",
        "2024-02-10T09:00:00Z",
        "2024-02-19T09:00:00Z",
        "2024-02-26T09:00:00Z",
        "2024-03-04T09:00:00Z",
    ]


def test_exdate_before_the_new_dtstart_is_dropped_and_later_ones_are_kept() -> None:
    """A now-unreachable EXDATE is redundant; a later one still excludes its occurrence."""
    pruned = _pruned(_prune_fixture("recurring_rdate_exdate.ics", date(2024, 2, 1)))

    text = _ical_text(pruned)
    assert "EXDATE:20240212T090000Z" in text
    assert "20240108T090000Z" not in text
    assert "2024-02-12T09:00:00Z" not in _starts(pruned)


def test_override_before_the_boundary_is_removed_and_a_later_one_is_kept() -> None:
    """Brief §5.4 step 2: a dead override is dropped as a whole component."""
    result = _prune_fixture("recurring_overrides.ics", date(2024, 2, 1))
    pruned = _pruned(result)

    text = _ical_text(pruned)
    assert "SUMMARY:Moved within the past" not in text
    assert "SUMMARY:Moved within the future" in text
    assert "RECURRENCE-ID:20240212T090000Z" in text
    assert (result.removed, result.kept) == (5, 5)
    assert _starts(pruned) == [
        "2024-02-05T09:00:00Z",
        "2024-02-13T14:00:00Z",
        "2024-02-19T09:00:00Z",
        "2024-02-26T09:00:00Z",
        "2024-03-04T09:00:00Z",
    ]


def test_straddling_occurrence_survives_whole_and_becomes_the_new_dtstart() -> None:
    """Brief §5.4 step 3: an occurrence spanning DATE is kept, never trimmed."""
    result = _prune_fixture("recurring_straddling.ics", date(2024, 1, 30))
    pruned = _pruned(result)

    text = _ical_text(pruned)
    assert "DTSTART:20240129T220000Z" in text
    assert "DTEND:20240130T060000Z" in text
    assert (result.removed, result.kept) == (4, 6)
    assert _starts(pruned)[0] == "2024-01-29T22:00:00Z"


def test_floating_series_is_pruned_in_the_resolved_zone() -> None:
    """Brief §5.1: a floating series is anchored in the run's zone, not in UTC.

    The fixture starts at a floating 23:00 on 2024-01-01 and runs to 00:30 the
    next day. Read in Europe/Rome against a 2024-02-01 boundary, the
    2024-01-29 occurrence is entirely in January and goes, while the
    2024-02-05 one starts the new series.
    """
    result = _prune_fixture("recurring_floating.ics", date(2024, 2, 1), _ROME)
    pruned = _pruned(result)

    text = _ical_text(pruned)
    assert "DTSTART:20240205T230000" in text
    assert "RRULE:FREQ=WEEKLY;UNTIL=20240219T230000" in text
    assert (result.removed, result.kept) == (5, 3)
    assert _starts(pruned) == [
        "2024-02-05T23:00:00",
        "2024-02-12T23:00:00",
        "2024-02-19T23:00:00",
    ]


def test_boundary_on_a_dst_transition_keeps_wall_clock_times() -> None:
    """A TZID series recurs by wall clock, so its UTC offset changes across DST.

    The fixture recurs at 09:00 Europe/Rome; the boundary of 2024-03-31 is the
    day Italy moves to CEST. Occurrences before it are 08:00Z, after it 07:00Z,
    and the converted ``UNTIL`` — which RFC 5545 requires in UTC — must carry
    the *post*-transition offset of the original last occurrence.
    """
    result = _prune_fixture("recurring_dst_boundary.ics", date(2024, 3, 31), _ROME)
    pruned = _pruned(result)

    text = _ical_text(pruned)
    assert "DTSTART;TZID=Europe/Rome:20240405T090000" in text
    assert "DTEND;TZID=Europe/Rome:20240405T100000" in text
    assert "RRULE:FREQ=WEEKLY;UNTIL=20240503T070000Z" in text
    assert (result.removed, result.kept) == (5, 5)
    assert _starts(pruned) == [
        "2024-04-05T07:00:00Z",
        "2024-04-12T07:00:00Z",
        "2024-04-19T07:00:00Z",
        "2024-04-26T07:00:00Z",
        "2024-05-03T07:00:00Z",
    ]


def test_unbounded_series_is_shifted_and_stays_unbounded() -> None:
    """Brief §5.4 step 6: an endless series keeps its endless RRULE after pruning."""
    result = _prune_fixture("recurring_unbounded_weekly_2015.ics", date(2024, 2, 1))
    pruned = _pruned(result)

    text = _ical_text(pruned)
    assert "DTSTART:20240205T090000Z" in text
    assert "RRULE:FREQ=WEEKLY" in text
    assert not any(
        line.startswith("RRULE") and ("UNTIL" in line or "COUNT" in line) for line in _lines(pruned)
    )
    assert result.strategy is PruneStrategy.DTSTART_SHIFT

    starts = _starts(pruned)
    assert starts[:3] == [
        "2024-02-05T09:00:00Z",
        "2024-02-12T09:00:00Z",
        "2024-02-19T09:00:00Z",
    ]
    assert all(start >= "2024-02-05" for start in starts)


def test_resource_whose_every_occurrence_is_over_is_deleted() -> None:
    """Brief §5.4 step 1: nothing survives, so the whole resource goes."""
    result = _prune_fixture("recurring_all_past.ics", date(2024, 2, 1))

    assert result.action is Action.DELETE
    assert result.calendar is None
    assert (result.removed, result.kept) == (5, 0)


def test_resource_with_no_affected_occurrence_is_untouched() -> None:
    """Brief §5.4 step 1: nothing is removed, so nothing is written."""
    result = _prune_fixture("recurring_all_future.ics", date(2024, 2, 1))

    assert result.action is Action.UNTOUCHED
    assert result.calendar is None
    assert (result.removed, result.kept) == (0, 5)


def test_orphan_overrides_are_pruned_without_a_master() -> None:
    """A resource made only of overrides has no DTSTART to shift; the dead ones go."""
    result = _prune_fixture("recurring_orphan_overrides.ics", date(2024, 2, 1))
    pruned = _pruned(result)

    assert result.strategy is PruneStrategy.OVERRIDE_REMOVAL
    assert (result.removed, result.kept) == (1, 1)
    text = _ical_text(pruned)
    assert "SUMMARY:Orphan override in the past" not in text
    assert "SUMMARY:Orphan override in the future" in text
    assert _starts(pruned) == ["2024-03-08T09:00:00Z"]


def test_validation_failure_falls_back_to_exdate_only_pruning() -> None:
    """Brief §5.4 step 4: when the shift cannot reproduce the survivors, EXDATE wins.

    The fixture's 2024-02-19 occurrence was overridden to 2024-01-20, which is
    before the boundary, so it must go while the 2024-02-05 and 2024-02-12
    ones stay. No ``DTSTART`` shift can express that — moving ``DTSTART`` to
    2024-02-05 leaves the rule generating 2024-02-19 again, now unoverridden —
    so the shifted candidate fails the validation gate and the ``EXDATE``-only
    version is written instead, with ``DTSTART`` and ``COUNT`` untouched.
    """
    result = _prune_fixture("recurring_override_pulled_back.ics", date(2024, 2, 1))
    pruned = _pruned(result)

    assert result.strategy is PruneStrategy.EXDATE_ONLY
    assert (result.removed, result.kept) == (6, 4)

    text = _ical_text(pruned)
    assert "DTSTART:20240101T090000Z" in text
    assert "RRULE:FREQ=WEEKLY;COUNT=10" in text
    assert "SUMMARY:Pulled back into the past" not in text

    exdates = text.split("EXDATE:", 1)[1].split("\r\n", 1)[0].split(",")
    assert exdates == [
        "20240101T090000Z",
        "20240108T090000Z",
        "20240115T090000Z",
        "20240122T090000Z",
        "20240129T090000Z",
        "20240219T090000Z",
    ]
    assert _starts(pruned) == [
        "2024-02-05T09:00:00Z",
        "2024-02-12T09:00:00Z",
        "2024-02-26T09:00:00Z",
        "2024-03-04T09:00:00Z",
    ]


def test_resource_that_cannot_be_pruned_safely_is_skipped() -> None:
    """Brief §5.4 step 4: an unsupported shape is reported, never written blindly."""
    result = _prune_fixture("recurring_period_rdate.ics", date(2024, 2, 1))

    assert result.action is Action.SKIP_UNPRUNABLE
    assert result.calendar is None


def test_pruning_never_mutates_the_calendar_it_was_given() -> None:
    """The engine is pure: every result is a copy, whatever strategy was taken."""
    for fixture_name in (
        "recurring_count_weekly.ics",
        "recurring_override_pulled_back.ics",
        "recurring_all_past.ics",
        "recurring_period_rdate.ics",
    ):
        calendar = _load(fixture_name)
        before = calendar.to_ical()

        prune_recurring(calendar, date(2024, 2, 1), UTC, now=_NOW)

        assert calendar.to_ical() == before, fixture_name


def test_pruning_keeps_exactly_the_occurrences_that_reach_the_boundary() -> None:
    """Property check: the survivors of the original are exactly what is left.

    The expected set is derived with :func:`_ends_before`, a hand-written
    oracle, so this asserts the engine against the rule from the brief rather
    than against another call to the expander.
    """
    for fixture_name, boundary, zone in (
        ("recurring_count_weekly.ics", date(2024, 2, 1), UTC),
        ("recurring_rdate_exdate.ics", date(2024, 2, 1), UTC),
        ("recurring_overrides.ics", date(2024, 2, 1), UTC),
        ("recurring_all_day_weekly.ics", date(2024, 2, 1), UTC),
        ("recurring_duration.ics", date(2024, 2, 1), UTC),
        ("recurring_straddling.ics", date(2024, 1, 30), UTC),
        ("recurring_override_pulled_back.ics", date(2024, 2, 1), UTC),
        ("recurring_orphan_overrides.ics", date(2024, 2, 1), UTC),
        ("recurring_dst_boundary.ics", date(2024, 3, 31), _ROME),
        ("recurring_floating.ics", date(2024, 2, 1), _ROME),
    ):
        original = _load(fixture_name)
        expected = sorted(
            _render(occurrence.start)
            for occurrence in expand_occurrences(original, _WINDOW_START, _WINDOW_END)
            if not _ends_before(occurrence.end, boundary, zone)
        )

        result = prune_recurring(original, boundary, zone, now=_NOW)

        assert result.action is Action.MODIFY, fixture_name
        assert result.calendar is not None
        assert _starts(result.calendar) == expected, fixture_name


def test_explicit_until_is_left_alone_by_the_shift() -> None:
    """Brief §5.4 step 2: an ``RRULE`` already bounded by ``UNTIL`` needs no conversion.

    The fixture is the ``UNTIL``-expressed twin of ``recurring_count_weekly``
    (same ten Monday occurrences, same boundary): only the right-hand
    ``UNTIL`` must survive unrecomputed while ``DTSTART`` shifts underneath it,
    per the brief's note that ``UNTIL`` "is not the boundary this command
    touches".
    """
    result = _prune_fixture("recurring_until_weekly.ics", date(2024, 2, 1))
    pruned = _pruned(result)

    text = _ical_text(pruned)
    assert "RRULE:FREQ=WEEKLY;UNTIL=20240304T090000Z" in text
    assert "DTSTART:20240205T090000Z" in text
    assert (result.removed, result.kept) == (5, 5)
    assert _starts(pruned) == [
        "2024-02-05T09:00:00Z",
        "2024-02-12T09:00:00Z",
        "2024-02-19T09:00:00Z",
        "2024-02-26T09:00:00Z",
        "2024-03-04T09:00:00Z",
    ]


def test_bysetpos_rule_is_shifted_to_the_first_surviving_occurrence() -> None:
    """A ``BYSETPOS`` rule ("last weekday of the month") shifts like any other.

    The fixture runs monthly on the last weekday from 2024-01-31 for six
    occurrences (2024-01-31, 02-29, 03-29, 04-30, 05-31, 06-28 — verified by
    hand against a calendar, independently of both ``recurring-ical-events``
    and the engine under test). Pruning before 2024-04-01 drops the first
    three; the ``BYSETPOS`` selection is computed per month, so shifting
    ``DTSTART`` to 2024-04-30 does not perturb which day the rule picks in the
    months that follow.
    """
    result = _prune_fixture("recurring_bysetpos_monthly.ics", date(2024, 4, 1))
    pruned = _pruned(result)

    assert result.strategy is PruneStrategy.DTSTART_SHIFT
    text = _ical_text(pruned)
    assert "DTSTART:20240430T090000Z" in text
    assert "RRULE:FREQ=MONTHLY;UNTIL=20240628T090000Z;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1" in text
    assert not any(line.startswith("RRULE") and "COUNT" in line for line in _lines(pruned))
    assert (result.removed, result.kept) == (3, 3)
    assert _starts(pruned) == [
        "2024-04-30T09:00:00Z",
        "2024-05-31T09:00:00Z",
        "2024-06-28T09:00:00Z",
    ]


def test_recurrence_id_matches_master_instant_across_tzid_and_utc_form() -> None:
    """A ``RECURRENCE-ID`` may be written in UTC even when the master carries a ``TZID``.

    RFC 5545 matches ``RECURRENCE-ID`` to the master's generated instant by
    moment, not by literal representation. The fixture's master recurs at
    09:00 ``Europe/Rome`` (08:00 UTC, no DST in play before the March
    transition); its override's ``RECURRENCE-ID`` is written as
    ``20240205T080000Z`` instead of the equivalent ``TZID`` form, and pulls
    that occurrence back into January. If the engine failed to recognise the
    two as the same instant, the override would not be matched to the 2024-02-05
    rule occurrence, that occurrence would be judged by the master's nominal
    (unmoved) time instead, and it would wrongly survive pruning.
    """
    result = _prune_fixture("recurring_recurrence_id_tzid_equivalent.ics", date(2024, 2, 1))
    pruned = _pruned(result)

    assert result.strategy is PruneStrategy.DTSTART_SHIFT
    text = _ical_text(pruned)
    assert "DTSTART;TZID=Europe/Rome:20240212T090000" in text
    assert "RRULE:FREQ=WEEKLY;UNTIL=20240304T080000Z" in text
    assert "Pulled back via UTC-form RECURRENCE-ID" not in text
    assert not any(line.startswith("RECURRENCE-ID") for line in _lines(pruned))
    assert (result.removed, result.kept) == (6, 4)
    assert _starts(pruned) == [
        "2024-02-12T08:00:00Z",
        "2024-02-19T08:00:00Z",
        "2024-02-26T08:00:00Z",
        "2024-03-04T08:00:00Z",
    ]


def test_folded_escaped_non_ascii_text_survives_pruning_verbatim() -> None:
    r"""Line folding, RFC 5545 escaping, and non-ASCII text are not the engine's business.

    The fixture's ``SUMMARY`` is a folded, non-ASCII line and its
    ``DESCRIPTION`` uses every general escape (``\\,``, ``\\;``, ``\\n``).
    Pruning must round-trip both verbatim: the ``DTSTART`` shift touches only
    ``DTSTART``/``DTEND``/``RRULE``/the revision stamps, per brief §5.4 step 5.
    """
    result = _prune_fixture("recurring_unicode_folding.ics", date(2024, 2, 1))
    pruned = _pruned(result)

    text = _ical_text(pruned)
    assert (
        "SUMMARY:Réunion café ☕ — revue trimestrielle : budget\\, feuille de route "
        "et recrutement pour l'équipe internationale" in text
    )
    assert "DESCRIPTION:Line one\\, with comma\\; and semicolon\\nLine two continues here" in text
    assert (result.removed, result.kept) == (5, 5)
    assert _starts(pruned) == [
        "2024-02-05T09:00:00Z",
        "2024-02-12T09:00:00Z",
        "2024-02-19T09:00:00Z",
        "2024-02-26T09:00:00Z",
        "2024-03-04T09:00:00Z",
    ]
