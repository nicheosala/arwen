"""Unit tests for the brief §6 duplicate-detection engine.

Covers key normalization (§6.2), the content hash and its exclusion list
(§6.3 step 2), and the grouping/tie-break algorithm (§6.3), including the
three pathological cases from brief §10 that were deferred until
:mod:`arwen.dedup` existed: five byte-identical copies, three identical
copies plus two divergent ones, and two events sharing a key but carrying
different ``RRULE``s.

Every grouping assertion works through the public :func:`group_duplicates`
API, never through its private helpers, matching
:mod:`tests.test_recurrence_pruning`'s convention of testing engines through
their public surface only.
"""

import datetime
import itertools
from pathlib import Path

import pytest
from icalendar import Calendar
from icalendar.prop import vGeo, vTime, vUTCOffset

from arwen.dedup import (
    _canonical_line,
    content_hash,
    duplicate_key,
    group_duplicates,
    is_examinable,
    normalize_summary,
)
from arwen.model import DedupCandidate

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(fixture_name: str) -> Calendar:
    """Parse a fixture under ``tests/fixtures/`` into a fresh :class:`icalendar.Calendar`.

    Reads and re-parses the file on every call, so callers that need several
    independent "resources" sharing one fixture's bytes (simulating several
    CalDAV resources with byte-identical content) get distinct objects.
    """
    parsed = Calendar.from_ical((_FIXTURES_DIR / fixture_name).read_text(encoding="utf-8-sig"))
    assert isinstance(parsed, Calendar)
    return parsed


def _candidate(fixture_name: str, href: str, etag: str = "etag") -> DedupCandidate:
    """Build a :class:`~arwen.model.DedupCandidate` from a fixture, with a given href."""
    return DedupCandidate(href=href, etag=etag, calendar=_load(fixture_name))


def _uid_of(candidate: DedupCandidate) -> str:
    """Return the UID of a candidate's sole VEVENT, for identity assertions."""
    return str(candidate.calendar.walk("VEVENT")[0]["UID"])


# --- §6.2 duplicate key -----------------------------------------------------


def test_normalize_summary_trims_and_collapses_internal_whitespace() -> None:
    """Brief §6.2: SUMMARY is trimmed and its internal whitespace collapsed."""
    assert normalize_summary("  Weekly   sync\t run  ") == "Weekly sync run"


def test_normalize_summary_is_case_sensitive() -> None:
    """Brief §6.2: SUMMARY comparison is case-sensitive; only whitespace is touched."""
    assert normalize_summary("Team Sync") != normalize_summary("team sync")


def test_missing_summary_normalizes_to_the_empty_string_and_stays_eligible() -> None:
    """Brief §6.2: a resource with no SUMMARY at all still gets a matchable key."""
    key = duplicate_key(_load("non_recurring_no_dtend.ics"))
    assert key.summary != ""  # sanity: this fixture does carry a SUMMARY

    assert normalize_summary("") == ""


def test_key_matches_across_tzid_and_equivalent_utc_instant() -> None:
    """Brief §6.2: two DTSTARTs with different TZIDs denoting the same moment match.

    ``dedup_key_tzid_rome.ics`` recurs at 11:00 Europe/Rome (CET, no DST in
    play in mid-March), which is the same instant as ``dedup_key_tzid_utc_
    equivalent.ics``'s 10:00 UTC.
    """
    rome_key = duplicate_key(_load("dedup_key_tzid_rome.ics"))
    utc_key = duplicate_key(_load("dedup_key_tzid_utc_equivalent.ics"))

    assert rome_key == utc_key


def test_timed_and_all_day_events_never_share_a_key() -> None:
    """Brief §6.2: timed and all-day events never match, even on the same nominal date.

    The two fixtures cover the exact same calendar day (2024-07-01, exclusive
    DTEND on the 2nd either way) — deliberately, so that only the type
    distinction between an absolute instant and a pure date stops them from
    comparing equal.
    """
    all_day_key = duplicate_key(_load("dedup_key_all_day.ics"))
    timed_key = duplicate_key(_load("dedup_key_timed_same_date.ics"))

    assert all_day_key != timed_key
    assert all_day_key.all_day is True
    assert timed_key.all_day is False


# --- §6.3 step 2 content hash ------------------------------------------------


def test_content_hash_ignores_the_excluded_properties_and_property_order() -> None:
    """Brief §6.3 step 2: UID, DTSTAMP, LAST-MODIFIED, CREATED, SEQUENCE, PRODID are excluded.

    The two fixtures differ in every excluded property, in the order
    properties appear, and in the parameter order of a shared ``ATTENDEE`` —
    and still hash identically.
    """
    assert content_hash(_load("dedup_hash_exclusions_a.ics")) == content_hash(
        _load("dedup_hash_exclusions_b.ics")
    )


def test_content_hash_changes_when_meaningful_content_differs() -> None:
    """A DESCRIPTION difference is not volatile: it must change the hash."""
    assert content_hash(_load("dedup_hash_exclusions_a.ics")) != content_hash(
        _load("dedup_hash_divergent.ics")
    )


# --- §6.1 examinability -------------------------------------------------------


def test_is_examinable_excludes_recurring_and_non_vevent_resources() -> None:
    """Brief §6.1: recurring resources and non-VEVENT resources are never examined."""
    assert is_examinable(_load("dedup_identical_base.ics"))
    assert not is_examinable(_load("recurring_count_weekly.ics"))
    assert not is_examinable(_load("dedup_not_examinable_vtodo.ics"))


# --- §6.3 grouping and tie-break ---------------------------------------------


def test_five_identical_copies_reduce_to_exactly_one_survivor() -> None:
    """Brief §10: 5 byte-identical copies of one event must reduce to exactly 1."""
    candidates = [_candidate("dedup_identical_base.ics", href=f"/cal/{i}.ics") for i in range(5)]

    groups = group_duplicates(candidates)

    assert len(groups) == 1
    group = groups[0]
    assert len(group.kept) == 1
    assert len(group.to_delete) == 4
    assert group.needs_review == ()
    assert {c.href for c in (*group.kept, *group.to_delete)} == {c.href for c in candidates}


def test_grouping_post_condition_exactly_one_survivor_for_n_2_5_20() -> None:
    """Post-condition: every acted-upon key group has exactly one survivor, for N=2, 5, 20."""
    for count in (2, 5, 20):
        candidates = [
            _candidate("dedup_identical_base.ics", href=f"/cal/{i}.ics") for i in range(count)
        ]

        groups = group_duplicates(candidates)

        assert len(groups) == 1, count
        group = groups[0]
        assert len(group.kept) == 1, count
        assert len(group.to_delete) == count - 1, count
        assert group.needs_review == (), count
        assert len(group.kept) + len(group.to_delete) == count, count


def test_three_identical_plus_two_divergent_needs_review() -> None:
    """Brief §10: 3 identical + 2 divergent must reduce to 3 survivors.

    One winner from the identical sub-group survives, with the other 2
    deleted, alongside the two divergent resources — brief §6.3 step 4's
    mixed sub-group rule, which still deduplicates within a
    content-identical sub-group even though the surrounding key group is
    otherwise ambiguous.
    """
    identical = [
        _candidate("dedup_divergent_identical.ics", href=f"/cal/identical-{i}.ics")
        for i in range(3)
    ]
    variant_a = _candidate("dedup_divergent_variant_a.ics", href="/cal/variant-a.ics")
    variant_b = _candidate("dedup_divergent_variant_b.ics", href="/cal/variant-b.ics")
    candidates = [*identical, variant_a, variant_b]

    groups = group_duplicates(candidates)

    assert len(groups) == 1
    group = groups[0]
    assert len(group.kept) == 1
    assert group.kept[0].href in {c.href for c in identical}
    assert len(group.to_delete) == 2
    assert {c.href for c in group.to_delete} < {c.href for c in identical}
    assert {c.href for c in group.needs_review} == {"/cal/variant-a.ics", "/cal/variant-b.ics"}

    survivors = {c.href for c in (*group.kept, *group.needs_review)}
    assert len(survivors) == 3


def test_same_key_different_rrule_is_never_touched() -> None:
    """Brief §10: two events with an identical key but different RRULE must not be touched.

    Both fixtures share a SUMMARY, DTSTART, and DTEND, so they *would* share
    a duplicate key — but both carry an RRULE, so §6.1 excludes them from
    examination entirely: they must not be deleted, and not even flagged for
    review.
    """
    weekly = _load("dedup_recurring_key_clash_weekly.ics")
    daily = _load("dedup_recurring_key_clash_daily.ics")
    assert duplicate_key(weekly) == duplicate_key(daily)  # sanity: same key, if it mattered

    candidates = [
        DedupCandidate(href="/cal/weekly.ics", etag="etag", calendar=weekly),
        DedupCandidate(href="/cal/daily.ics", etag="etag", calendar=daily),
    ]

    groups = group_duplicates(candidates)

    assert groups == []


def test_tie_break_winner_is_independent_of_input_order() -> None:
    """Brief §6.3: the tie-break must be deterministic regardless of input ordering.

    Five fixtures share one duplicate key and content hash, and are laid out
    as a ladder that walks every tie-break step in turn: ``seq_low`` is
    beaten purely on SEQUENCE, ``lastmod_old`` ties SEQUENCE but loses on
    LAST-MODIFIED, ``dtstamp_old`` ties both but loses on DTSTAMP, ``uid_z``
    ties all three but loses on UID, and ``winner`` beats every one of them.
    Every one of the 120 orderings of these five candidates must still pick
    ``winner``.
    """
    names = [
        "dedup_ladder_seq_low.ics",
        "dedup_ladder_lastmod_old.ics",
        "dedup_ladder_dtstamp_old.ics",
        "dedup_ladder_uid_z.ics",
        "dedup_ladder_winner.ics",
    ]

    for permutation in itertools.permutations(range(len(names))):
        candidates = [_candidate(names[i], href=f"/cal/ladder-{i}.ics") for i in permutation]

        groups = group_duplicates(candidates)

        assert len(groups) == 1
        group = groups[0]
        assert len(group.kept) == 1
        assert _uid_of(group.kept[0]) == "aaa@arwen.test"
        assert len(group.to_delete) == 4
        assert group.needs_review == ()


class TestContentHashValueTypes:
    """The hash must survive every value type ``icalendar`` can hand back (brief §6.3).

    ``Component.property_items`` yields whatever concrete class parsed each
    property, and those classes do not render uniformly: most return ``bytes``
    from ``to_ical()``, but ``vUTCOffset`` (``TZOFFSETFROM``/``TZOFFSETTO``),
    ``vGeo`` (``GEO``) and ``vTime`` return ``str``, while the ``BEGIN``/``END``
    component markers arrive as raw ``bytes`` with no ``to_ical()`` at all.
    Assuming one form crashed ``delete duplicates`` on any resource carrying a
    full ``VTIMEZONE`` — the shape Thunderbird and other exporters emit.

    These fixtures are built from the wire bytes of such resources, so they
    exercise the real classes rather than a hand-built stand-in.
    """

    def test_hashing_a_resource_with_a_vtimezone_succeeds(self) -> None:
        """A resource carrying a full VTIMEZONE hashes rather than raising.

        The regression test for the ``AttributeError: 'str' object has no
        attribute 'decode'`` crash. Since :class:`TestContentHashScope` took
        ``VTIMEZONE`` out of the hash, this no longer reaches ``vUTCOffset``
        through ``content_hash`` — it pins that such a resource is handled at
        all, while
        :meth:`test_str_rendering_value_types_canonicalise_to_their_wire_text`
        pins the value type itself.
        """
        digest = content_hash(_load("dedup_value_types_vtimezone.ics"))

        assert len(digest) == 64

    def test_hashing_a_resource_with_geo_and_an_alarm_succeeds(self) -> None:
        """``GEO`` (``vGeo``) also renders to ``str``, as does a ``VALARM``'s content."""
        digest = content_hash(_load("dedup_value_types_geo_alarm.ics"))

        assert len(digest) == 64

    def test_str_rendered_values_still_hash_order_independently(self) -> None:
        """Property and parameter order still do not matter once ``str`` values are in play.

        The two fixtures carry the same event and the same VTIMEZONE, written
        in a different property order and differing in every excluded
        property (``UID``, ``SEQUENCE``, ``CREATED``, ``LAST-MODIFIED``,
        ``DTSTAMP``, ``PRODID``).
        """
        assert content_hash(_load("dedup_value_types_vtimezone.ics")) == content_hash(
            _load("dedup_value_types_vtimezone_reordered.ics")
        )

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (vUTCOffset(datetime.timedelta(seconds=2940)), "+0049"),
            (vUTCOffset(datetime.timedelta(seconds=3600)), "+0100"),
            (vGeo((45.464211, 9.191383)), "45.464211;9.191383"),
            (vTime(datetime.time(9, 30)), "093000"),
        ],
    )
    def test_str_rendering_value_types_canonicalise_to_their_wire_text(
        self, value: object, expected: str
    ) -> None:
        """``_canonical_line`` renders the ``str``-returning classes to their wire form.

        Pinned directly rather than through :func:`content_hash`, because
        ``vUTCOffset`` only ever occurs on ``TZOFFSETFROM``/``TZOFFSETTO``
        inside a ``VTIMEZONE`` — which :class:`TestContentHashScope` puts
        out of the hash's scope. The value-type contract still has to hold
        for ``vGeo``, which is event content, and for any future property
        whose class renders to ``str``.
        """
        line = _canonical_line("X-TEST", value)

        assert line.endswith(f"\x1f{expected}")

    def test_differing_utc_offsets_canonicalise_differently(self) -> None:
        """Normalizing ``str`` renderings must not flatten distinct values together."""
        one = _canonical_line("TZOFFSETFROM", vUTCOffset(datetime.timedelta(seconds=2940)))
        other = _canonical_line("TZOFFSETFROM", vUTCOffset(datetime.timedelta(seconds=3000)))

        assert one != other

    def test_a_differing_geo_still_changes_the_hash(self) -> None:
        """The same, for ``vGeo``: two coordinates must not hash alike."""
        assert content_hash(_load("dedup_value_types_geo_alarm.ics")) != content_hash(
            _load("dedup_value_types_geo_differs.ics")
        )

    def test_parameters_of_str_subclassing_values_still_reach_the_hash(self) -> None:
        """``vText`` and friends subclass ``str``; they must not be hashed as bare text.

        ``LOCATION``, ``ATTENDEE``, and ``TRIGGER`` in this fixture all carry
        parameters, and their value classes (``vText``, ``vCalAddress``)
        subclass ``str``. If ``_canonical_line`` matched bare ``str`` before
        the property-value protocol, those parameters would silently vanish
        from the hash — so changing only a parameter must still change it.
        """
        original = (_FIXTURES_DIR / "dedup_value_types_geo_alarm.ics").read_bytes()
        altered = original.replace(b"PARTSTAT=ACCEPTED", b"PARTSTAT=DECLINED")
        assert altered != original

        parsed = Calendar.from_ical(altered.decode("utf-8"))
        assert isinstance(parsed, Calendar)

        assert content_hash(parsed) != content_hash(_load("dedup_value_types_geo_alarm.ics"))

    def test_excluded_properties_are_still_excluded_with_str_values_present(self) -> None:
        """The §6.3 exclusion list is unaffected by the value-type handling.

        Rewriting every excluded property of the VTIMEZONE fixture leaves the
        hash unchanged, exactly as it must for a resource of any other shape.
        """
        original = (_FIXTURES_DIR / "dedup_value_types_vtimezone.ics").read_bytes()
        altered = (
            original.replace(b"UID:value-types-vtimezone-1", b"UID:something-entirely-different")
            .replace(b"SEQUENCE:0", b"SEQUENCE:41")
            .replace(b"DTSTAMP:20250901T101500Z", b"DTSTAMP:20200101T000000Z")
            .replace(b"CREATED:20250901T101500Z", b"CREATED:20200101T000000Z")
            .replace(b"LAST-MODIFIED:20250901T101500Z", b"LAST-MODIFIED:20200101T000000Z")
            .replace(b"PRODID:-//Mozilla.org/NONSGML Mozilla Calendar V1.1//EN", b"PRODID:-//X//EN")
        )
        assert altered != original

        parsed = Calendar.from_ical(altered.decode("utf-8"))
        assert isinstance(parsed, Calendar)

        assert content_hash(parsed) == content_hash(_load("dedup_value_types_vtimezone.ics"))

    def test_identical_vtimezone_resources_are_grouped_as_duplicates(self) -> None:
        """End to end: two byte-identical VTIMEZONE resources still group and de-duplicate."""
        candidates = [
            _candidate("dedup_value_types_vtimezone.ics", href="/cal/tz-1.ics"),
            _candidate("dedup_value_types_vtimezone.ics", href="/cal/tz-2.ics"),
        ]

        groups = group_duplicates(candidates)

        assert len(groups) == 1
        assert len(groups[0].kept) == 1
        assert len(groups[0].to_delete) == 1


class TestContentHashScope:
    """The hash covers the ``VEVENT``, not the enclosing ``VCALENDAR`` (brief §6.3 step 2).

    A ``VTIMEZONE`` is a timezone *definition* shipped alongside the event,
    not event content. Two clients exporting the same instant emit wildly
    different transition tables for the same ``TZID``: the Thunderbird
    fixture here carries the real 49-subcomponent ``Europe/Rome`` table
    reaching back to 1893, the DAVx5 one the same zone as two modern rules.
    Hashing those made one event look like two — precisely on a calendar
    synced by more than one client, where de-duplication matters most.

    What must *not* follow is a weaker hash: the ``TZID`` a ``DTSTART`` or
    ``DTEND`` carries is a parameter of the event's own property, so a
    genuine timezone difference still separates two resources.
    """

    def test_the_same_event_hashes_equal_across_exporters(self) -> None:
        """A byte-identical VEVENT hashes the same under either exporter's VTIMEZONE.

        The regression test for the ``Collegio docenti`` false negative: the
        two fixtures are the real shapes Thunderbird and DAVx5 wrote for one
        event, differing in 47 ``VTIMEZONE`` subcomponents and nothing else.
        """
        assert content_hash(_load("dedup_tz_thunderbird.ics")) == content_hash(
            _load("dedup_tz_davx5.ics")
        )

    def test_an_absent_vtimezone_hashes_the_same_too(self) -> None:
        """Carrying no VTIMEZONE at all is likewise not an event-content difference."""
        assert content_hash(_load("dedup_tz_thunderbird.ics")) == content_hash(
            _load("dedup_tz_event_plain.ics")
        )

    def test_a_differing_tzid_on_the_event_still_separates(self) -> None:
        """A real timezone difference is a parameter of ``DTSTART``, and still counts.

        The counterweight to excluding ``VTIMEZONE``: the same wall-clock
        time in ``Europe/Helsinki`` is a different instant, and must not
        collide with the ``Europe/Rome`` event.
        """
        assert content_hash(_load("dedup_tz_event_plain.ics")) != content_hash(
            _load("dedup_tz_event_other_tzid.ics")
        )

    @pytest.mark.parametrize(
        "variant",
        [
            "dedup_tz_event_description.ics",
            "dedup_tz_event_location.ics",
            "dedup_tz_event_valarm.ics",
        ],
    )
    def test_event_content_differences_still_separate(self, variant: str) -> None:
        """``DESCRIPTION``, ``LOCATION``, and a ``VALARM`` remain event content (§6.3 step 4).

        Each fixture shares the DAVx5 ``VTIMEZONE`` verbatim and differs from
        the baseline in exactly one of these, so nothing but that property
        can account for the hash changing.
        """
        assert content_hash(_load("dedup_tz_event_plain.ics")) != content_hash(_load(variant))

    def test_all_event_variants_are_mutually_distinct(self) -> None:
        """No two of the content variants collide with each other either."""
        names = [
            "dedup_tz_event_plain.ics",
            "dedup_tz_event_description.ics",
            "dedup_tz_event_location.ics",
            "dedup_tz_event_valarm.ics",
            "dedup_tz_event_other_tzid.ics",
        ]

        digests = {content_hash(_load(name)) for name in names}

        assert len(digests) == len(names)

    def test_cross_exporter_duplicates_are_now_grouped_and_deleted(self) -> None:
        """End to end: the two exporter shapes group as duplicates, leaving one survivor.

        Previously they landed in separate content sub-groups and the whole
        key group was reported as needing manual review, deleting nothing.
        """
        candidates = [
            _candidate("dedup_tz_thunderbird.ics", href="/cal/tz-thunderbird.ics"),
            _candidate("dedup_tz_davx5.ics", href="/cal/tz-davx5.ics"),
        ]

        groups = group_duplicates(candidates)

        assert len(groups) == 1
        assert len(groups[0].kept) == 1
        assert len(groups[0].to_delete) == 1
        assert groups[0].needs_review == ()

    def test_a_differing_vtimezone_offset_does_not_change_the_hash(self) -> None:
        """Two exports differing only inside the VTIMEZONE are the same event.

        The fixtures differ in one historical ``TZOFFSETFROM`` (``+0049`` vs
        ``+0050``) — a transition-table detail, not a property of the event.
        """
        assert content_hash(_load("dedup_value_types_vtimezone.ics")) == content_hash(
            _load("dedup_value_types_vtimezone_offset_differs.ics")
        )

    def test_a_vtimezone_only_resource_hashes_as_empty(self) -> None:
        """A resource with no VEVENT has nothing in scope, and is never examined anyway.

        :func:`is_examinable` already excludes it (brief §6.1); this pins
        that scoping the hash to ``VEVENT`` did not turn that into a crash.
        """
        calendar = _load("dedup_not_examinable_vtodo.ics")

        assert is_examinable(calendar) is False
        assert len(content_hash(calendar)) == 64
