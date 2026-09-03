"""End-to-end integration tests for both commands, against the in-process fake server.

Layer 2 of brief §10: assertions live on the resulting iCalendar data and on
the HTTP requests the fake server actually observed — never on internal call
counts. The property-style checks brief §10 asks to be encoded are all here:

- a dry run issues zero mutating HTTP requests, for both commands;
- no request that could mutate ever omits ``Schedule-Reply: F``;
- no operation ever increases the number of resources in the collection;
- after ``delete duplicates``, every acted-upon key has exactly one survivor;

together with the brief §7 safety model — a failing backup leaves the server
untouched, a ``412`` is a recorded conflict rather than a fatal error — and
the exit codes.
"""

import json
from datetime import date, datetime
from typing import TYPE_CHECKING

import pytest
from icalendar import Calendar
from icalendar.prop import vDDDTypes, vRecur

from arwen.backup import ETAG_PROPERTY, HREF_PROPERTY
from arwen.cli import (
    EXIT_CONNECTION,
    EXIT_FINDINGS,
    EXIT_OK,
    EXIT_USAGE,
    main,
)
from tests.fake_server import FakeCalDAVServer, FakeCollection, FakeResource

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from icalendar import Component

    from tests.fake_server import RecordedRequest

_READ_ONLY_METHODS = frozenset({"OPTIONS", "PROPFIND", "REPORT", "GET", "HEAD"})
"""Methods that cannot change server state; everything else must carry the §7 headers."""


def _start_datetime(event: Component) -> datetime:
    """Return an event's ``DTSTART`` as a date-time, narrowed for the type checker."""
    prop = event["DTSTART"]
    assert isinstance(prop, vDDDTypes)
    moment = prop.dt
    assert isinstance(moment, datetime)
    return moment


def _rrule(event: Component) -> vRecur:
    """Return an event's ``RRULE``, narrowed for the type checker."""
    rule = event["RRULE"]
    assert isinstance(rule, vRecur)
    return rule


def _until_date(rule: vRecur) -> date:
    """Return the calendar date of an ``RRULE``'s ``UNTIL``, narrowed for the type checker."""
    values = rule["UNTIL"]
    assert isinstance(values, list)
    moment = values[0]
    assert isinstance(moment, datetime)
    return moment.date()


def _event(
    uid: str,
    summary: str,
    dtstart: str,
    dtend: str,
    *,
    extra: str = "",
    sequence: int = 0,
    dtstamp: str = "20240101T000000Z",
) -> bytes:
    """Build a minimal single-``VEVENT`` ``.ics`` document."""
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//arwen tests//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{dtstamp}\r\n"
        f"SEQUENCE:{sequence}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DTSTART:{dtstart}\r\n"
        f"DTEND:{dtend}\r\n"
        f"{extra}"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode()


def _write_env(tmp_path: Path, server: FakeCalDAVServer) -> Path:
    """Write an ``arwen.env`` pointing at a running fake server."""
    path = tmp_path / "arwen.env"
    path.write_text(
        f"ARWEN_CALDAV_URL={server.base_url}\n"
        "ARWEN_CALDAV_USERNAME=user@example.org\n"
        "ARWEN_CALDAV_PASSWORD=s3cret\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _argv(env_file: Path, backup_dir: Path, *extra: str) -> list[str]:
    """Build an argument vector with the global options every test needs."""
    return [
        "--calendar",
        "Personal",
        "--env-file",
        str(env_file),
        "--backup-dir",
        str(backup_dir),
        *extra,
    ]


def _mutating(requests: Sequence[RecordedRequest]) -> list[RecordedRequest]:
    """Return every recorded request whose method could change server state."""
    return [request for request in requests if request.method not in _READ_ONLY_METHODS]


def _assert_schedule_reply_on_every_mutation(server: FakeCalDAVServer) -> None:
    """Assert the CLAUDE.md invariant on the requests the server actually observed.

    Every request that is not read-only must carry ``Schedule-Reply: F``
    (RFC 6638) so no attendee is ever notified, and ``If-Match`` with the
    ETag read during the scan (brief §7).
    """
    for request in _mutating(server.requests):
        assert request.headers.get("schedule-reply") == "F", request
        assert request.headers.get("if-match"), request


@pytest.fixture
def past_present_future() -> Iterator[FakeCalDAVServer]:
    """A calendar with a finished event, a straddling one, a series, and a future one."""
    collection = FakeCollection(name="personal", display_name="Personal")
    collection.add(
        FakeResource(
            name="past.ics",
            ics=_event("past@arwen.test", "Past", "20240101T090000Z", "20240101T100000Z"),
        )
    )
    collection.add(
        FakeResource(
            name="straddling.ics",
            ics=_event(
                "straddling@arwen.test", "Straddling", "20241230T090000Z", "20250102T100000Z"
            ),
        )
    )
    collection.add(
        FakeResource(
            name="weekly.ics",
            ics=_event(
                "weekly@arwen.test",
                "Weekly stand-up",
                "20240101T090000Z",
                "20240101T093000Z",
                extra="RRULE:FREQ=WEEKLY;COUNT=104\r\n",
            ),
        )
    )
    collection.add(
        FakeResource(
            name="future.ics",
            ics=_event("future@arwen.test", "Future", "20270101T090000Z", "20270101T100000Z"),
        )
    )
    server = FakeCalDAVServer(collections=[collection])
    with server:
        yield server


def _duplicate_collection(copies: int, *, divergent: int = 0) -> FakeCollection:
    """Build a collection of ``copies`` byte-identical events plus ``divergent`` variants.

    Only the ``UID`` differs between the identical copies — it is excluded
    from the content hash (brief §6.3 step 2) and is the last tie-break
    step, so the winner is fully determined. Each divergent variant shares
    the duplicate key but carries a different ``LOCATION``, which is exactly
    the "not interchangeable" case of brief §6.3 step 4.
    """
    collection = FakeCollection(name="personal", display_name="Personal")
    for index in range(copies):
        collection.add(
            FakeResource(
                name=f"copy{index}.ics",
                ics=_event(
                    f"copy-{index:03d}@arwen.test",
                    "Sprint planning",
                    "20240410T140000Z",
                    "20240410T150000Z",
                ),
            )
        )
    for index in range(divergent):
        collection.add(
            FakeResource(
                name=f"variant{index}.ics",
                ics=_event(
                    f"variant-{index}@arwen.test",
                    "Sprint planning",
                    "20240410T140000Z",
                    "20240410T150000Z",
                    extra=f"LOCATION:Room {index}\r\n",
                ),
            )
        )
    return collection


class TestDryRunIssuesNoMutations:
    """Brief §10: a dry run issues zero mutating HTTP requests, for both commands."""

    def test_delete_before_dry_run_issues_no_mutating_requests(
        self,
        past_present_future: FakeCalDAVServer,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A dry run plans deletes and modifies, yet sends no PUT or DELETE at all."""
        env_file = _write_env(tmp_path, past_present_future)
        collection = past_present_future.collections["personal"]
        before = dict(collection.resources)

        code = main(["delete", "before", "2025-01-01", *_argv(env_file, tmp_path / "backups")])

        output = capsys.readouterr().out
        assert "DRY RUN" in output
        assert _mutating(past_present_future.requests) == []
        assert {name: r.ics for name, r in collection.resources.items()} == {
            name: r.ics for name, r in before.items()
        }
        assert not (tmp_path / "backups").exists()
        assert code == EXIT_FINDINGS  # the straddling event is a reported skip

    def test_delete_duplicates_dry_run_issues_no_mutating_requests(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A dry run over five identical copies plans four deletes and sends none."""
        server = FakeCalDAVServer(collections=[_duplicate_collection(5)])
        with server:
            env_file = _write_env(tmp_path, server)
            collection = server.collections["personal"]

            code = main(["delete", "duplicates", *_argv(env_file, tmp_path / "backups")])

            output = capsys.readouterr().out
            assert "DRY RUN" in output
            assert _mutating(server.requests) == []
            assert len(collection.resources) == 5
            assert not (tmp_path / "backups").exists()
            assert code == EXIT_OK

    def test_dry_run_still_reports_what_it_would_do(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The dry-run report names the four copies it would delete, having deleted none."""
        server = FakeCalDAVServer(collections=[_duplicate_collection(5)])
        with server:
            env_file = _write_env(tmp_path, server)

            main(["delete", "duplicates", *_argv(env_file, tmp_path / "backups", "--json")])

            payload = json.loads(capsys.readouterr().out)
            assert payload["dry_run"] is True
            assert payload["counts"]["to_delete"] == 4
            assert sum(1 for r in payload["resources"] if r["action"] == "keep") == 1


class TestScheduleReplyInvariant:
    """CLAUDE.md: no request that could mutate ever omits ``Schedule-Reply: F``."""

    def test_delete_before_execute_sets_the_header_on_every_mutation(
        self, past_present_future: FakeCalDAVServer, tmp_path: Path
    ) -> None:
        """Executing ``delete before`` issues a DELETE and a PUT, both fully headed."""
        env_file = _write_env(tmp_path, past_present_future)

        main(
            [
                "delete",
                "before",
                "2025-01-01",
                *_argv(env_file, tmp_path / "backups", "--execute"),
            ]
        )

        methods = {request.method for request in _mutating(past_present_future.requests)}
        assert methods == {"PUT", "DELETE"}
        _assert_schedule_reply_on_every_mutation(past_present_future)

    def test_delete_duplicates_execute_sets_the_header_on_every_mutation(
        self, tmp_path: Path
    ) -> None:
        """Executing ``delete duplicates`` issues four DELETEs, all fully headed."""
        server = FakeCalDAVServer(collections=[_duplicate_collection(5)])
        with server:
            env_file = _write_env(tmp_path, server)

            main(["delete", "duplicates", *_argv(env_file, tmp_path / "backups", "--execute")])

            mutations = _mutating(server.requests)
            assert [request.method for request in mutations] == ["DELETE"] * 4
            _assert_schedule_reply_on_every_mutation(server)


class TestDeleteBeforeExecute:
    """Brief §5 end to end, with ``--execute``."""

    def test_removes_the_past_prunes_the_series_and_keeps_the_rest(
        self, past_present_future: FakeCalDAVServer, tmp_path: Path
    ) -> None:
        """The finished event goes, the series is pruned, straddling and future survive."""
        env_file = _write_env(tmp_path, past_present_future)
        collection = past_present_future.collections["personal"]
        original_count = len(collection.resources)

        code = main(
            [
                "delete",
                "before",
                "2025-01-01",
                *_argv(env_file, tmp_path / "backups", "--execute", "--tz", "UTC"),
            ]
        )

        assert set(collection.resources) == {"straddling.ics", "weekly.ics", "future.ics"}
        assert len(collection.resources) <= original_count
        assert code == EXIT_FINDINGS  # the straddling event is a reported skip

    def test_the_pruned_series_keeps_only_occurrences_from_the_boundary_on(
        self, past_present_future: FakeCalDAVServer, tmp_path: Path
    ) -> None:
        """The written-back series starts on the first surviving Monday, with COUNT gone.

        ``DTSTART`` was 2024-01-01, so a weekly series of 104 occurrences
        runs to 2025-12-22. Pruning to 2025-01-01 must shift ``DTSTART`` to
        2025-01-06 and convert ``COUNT`` to an ``UNTIL`` computed from the
        *original* ``DTSTART`` — leaving ``COUNT=104`` in place would
        invent a year of occurrences that never existed (brief §5.4).
        """
        env_file = _write_env(tmp_path, past_present_future)
        collection = past_present_future.collections["personal"]

        main(
            [
                "delete",
                "before",
                "2025-01-01",
                *_argv(env_file, tmp_path / "backups", "--execute", "--tz", "UTC"),
            ]
        )

        calendar = Calendar.from_ical(collection.resources["weekly.ics"].ics)
        event = calendar.walk("VEVENT")[0]
        assert _start_datetime(event).date().isoformat() == "2025-01-06"
        rule = _rrule(event)
        assert "COUNT" not in rule
        assert _until_date(rule).isoformat() == "2025-12-22"

    def test_a_straddling_event_is_skipped_and_never_truncated(
        self, past_present_future: FakeCalDAVServer, tmp_path: Path
    ) -> None:
        """The event spanning the boundary is left byte-for-byte as it was (brief §5.3)."""
        env_file = _write_env(tmp_path, past_present_future)
        collection = past_present_future.collections["personal"]
        original = collection.resources["straddling.ics"].ics

        main(
            [
                "delete",
                "before",
                "2025-01-01",
                *_argv(env_file, tmp_path / "backups", "--execute", "--tz", "UTC"),
            ]
        )

        assert collection.resources["straddling.ics"].ics == original

    def test_the_resolved_timezone_is_always_reported(
        self,
        past_present_future: FakeCalDAVServer,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Brief §5.1: the zone the boundary was resolved in is printed on every run."""
        env_file = _write_env(tmp_path, past_present_future)

        main(
            [
                "delete",
                "before",
                "2025-01-01",
                *_argv(env_file, tmp_path / "backups", "--tz", "Pacific/Auckland"),
            ]
        )

        assert "Pacific/Auckland" in capsys.readouterr().out


class TestDeleteDuplicatesExecute:
    """Brief §6 end to end: exactly one survivor per acted-upon key."""

    @pytest.mark.parametrize("copies", [2, 5, 20])
    def test_identical_copies_reduce_to_exactly_one_survivor(
        self, copies: int, tmp_path: Path
    ) -> None:
        """For N = 2, 5, and 20 alike, one resource survives — never the pairwise two."""
        server = FakeCalDAVServer(collections=[_duplicate_collection(copies)], shuffle_seed=7)
        with server:
            env_file = _write_env(tmp_path, server)
            collection = server.collections["personal"]

            code = main(
                ["delete", "duplicates", *_argv(env_file, tmp_path / "backups", "--execute")]
            )

            assert len(collection.resources) == 1
            assert code == EXIT_OK

    def test_three_identical_plus_two_divergent_leaves_three_survivors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Brief §6.3 step 4: two copies go, three survive, and all three need review."""
        server = FakeCalDAVServer(collections=[_duplicate_collection(3, divergent=2)])
        with server:
            env_file = _write_env(tmp_path, server)
            collection = server.collections["personal"]

            code = main(
                [
                    "delete",
                    "duplicates",
                    *_argv(env_file, tmp_path / "backups", "--execute", "--json"),
                ]
            )

            payload = json.loads(capsys.readouterr().out)
            assert len(collection.resources) == 3
            assert payload["counts"]["to_delete"] == 2
            assert payload["counts"]["needs_review"] == 3
            assert code == EXIT_FINDINGS

    def test_recurring_resources_are_reported_as_not_deduplicated(self, tmp_path: Path) -> None:
        """Brief §6.1: a recurring resource is never grouped, however its key compares."""
        collection = _duplicate_collection(0)
        for index in range(2):
            collection.add(
                FakeResource(
                    name=f"series{index}.ics",
                    ics=_event(
                        f"series-{index}@arwen.test",
                        "Sprint planning",
                        "20240410T140000Z",
                        "20240410T150000Z",
                        extra="RRULE:FREQ=WEEKLY;COUNT=5\r\n",
                    ),
                )
            )
        server = FakeCalDAVServer(collections=[collection])
        with server:
            env_file = _write_env(tmp_path, server)

            code = main(
                ["delete", "duplicates", *_argv(env_file, tmp_path / "backups", "--execute")]
            )

            assert len(server.collections["personal"].resources) == 2
            assert _mutating(server.requests) == []
            assert code == EXIT_FINDINGS


class TestBackup:
    """Brief §7: the backup is written before the first mutating request, or nothing happens."""

    def test_backup_holds_the_pre_mutation_content_of_every_changed_resource(
        self, past_present_future: FakeCalDAVServer, tmp_path: Path
    ) -> None:
        """Both the deleted and the modified resource are saved as they were before the run."""
        env_file = _write_env(tmp_path, past_present_future)
        backup_dir = tmp_path / "backups"

        main(
            [
                "delete",
                "before",
                "2025-01-01",
                *_argv(env_file, backup_dir, "--execute", "--tz", "UTC"),
            ]
        )

        (written,) = list(backup_dir.iterdir())
        documents = Calendar.from_ical(written.read_bytes(), multiple=True)
        uids = {str(document.walk("VEVENT")[0]["UID"]) for document in documents}
        assert uids == {"past@arwen.test", "weekly@arwen.test"}
        for document in documents:
            assert str(document[HREF_PROPERTY])
            assert str(document[ETAG_PROPERTY])
        series = next(d for d in documents if "weekly" in str(d.walk("VEVENT")[0]["UID"]))
        # The backup holds the *original* series, not the pruned one written back.
        assert _rrule(series.walk("VEVENT")[0])["COUNT"] == [104]

    def test_a_failing_backup_leaves_the_server_untouched(
        self,
        past_present_future: FakeCalDAVServer,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """With an unwritable backup directory, not one mutating request is issued.

        ``--backup-dir`` points at an existing regular file, so creating the
        directory fails no matter which user the suite runs as.
        """
        env_file = _write_env(tmp_path, past_present_future)
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("occupied", encoding="utf-8")
        collection = past_present_future.collections["personal"]
        before = {name: resource.ics for name, resource in collection.resources.items()}

        code = main(
            [
                "delete",
                "before",
                "2025-01-01",
                *_argv(env_file, blocked, "--execute", "--tz", "UTC"),
            ]
        )

        assert _mutating(past_present_future.requests) == []
        assert {name: r.ics for name, r in collection.resources.items()} == before
        assert blocked.read_text(encoding="utf-8") == "occupied"
        assert "no request was sent to the server" in capsys.readouterr().err
        assert code == EXIT_FINDINGS

    def test_no_backup_is_written_when_there_is_nothing_to_mutate(self, tmp_path: Path) -> None:
        """A run that plans no mutation writes no backup file at all."""
        collection = FakeCollection(name="personal", display_name="Personal")
        collection.add(
            FakeResource(
                name="future.ics",
                ics=_event("future@arwen.test", "Future", "20270101T090000Z", "20270101T100000Z"),
            )
        )
        server = FakeCalDAVServer(collections=[collection])
        with server:
            env_file = _write_env(tmp_path, server)
            backup_dir = tmp_path / "backups"

            code = main(
                [
                    "delete",
                    "before",
                    "2025-01-01",
                    *_argv(env_file, backup_dir, "--execute", "--tz", "UTC"),
                ]
            )

            assert not backup_dir.exists()
            assert code == EXIT_OK


class TestConflictHandling:
    """Brief §7: a 412 is a recorded conflict, never a fatal error and never a blind retry."""

    def test_a_412_on_put_is_reported_and_the_run_continues(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The series that could not be pruned is reported as a conflict; the delete still lands."""
        collection = FakeCollection(name="personal", display_name="Personal")
        collection.add(
            FakeResource(
                name="past.ics",
                ics=_event("past@arwen.test", "Past", "20240101T090000Z", "20240101T100000Z"),
            )
        )
        collection.add(
            FakeResource(
                name="weekly.ics",
                ics=_event(
                    "weekly@arwen.test",
                    "Weekly",
                    "20240101T090000Z",
                    "20240101T093000Z",
                    extra="RRULE:FREQ=WEEKLY;COUNT=104\r\n",
                ),
            )
        )
        server = FakeCalDAVServer(collections=[collection], force_412_on_put=True)
        with server:
            env_file = _write_env(tmp_path, server)
            unchanged = collection.resources["weekly.ics"].ics

            code = main(
                [
                    "delete",
                    "before",
                    "2025-01-01",
                    *_argv(env_file, tmp_path / "backups", "--execute", "--tz", "UTC", "--json"),
                ]
            )

            payload = json.loads(capsys.readouterr().out)
            assert payload["counts"]["conflicts"] == 1
            assert payload["counts"]["to_delete"] == 1
            assert collection.resources["weekly.ics"].ics == unchanged
            assert "past.ics" not in collection.resources
            assert code == EXIT_FINDINGS


class TestReportShape:
    """Brief §8: the same information and the same shape in dry-run and execute mode."""

    def test_json_keys_are_identical_in_both_modes(
        self,
        past_present_future: FakeCalDAVServer,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Only the ``dry_run`` flag distinguishes the two reports' shapes."""
        env_file = _write_env(tmp_path, past_present_future)
        argv = _argv(env_file, tmp_path / "backups", "--json", "--tz", "UTC")

        main(["delete", "before", "2025-01-01", *argv])
        dry = json.loads(capsys.readouterr().out)
        main(["delete", "before", "2025-01-01", *argv, "--execute"])
        executed = json.loads(capsys.readouterr().out)

        assert dry.keys() == executed.keys()
        assert dry["counts"].keys() == executed["counts"].keys()
        assert dry["dry_run"] is True
        assert executed["dry_run"] is False
        assert {r["action"] for r in dry["resources"]} == {
            r["action"] for r in executed["resources"]
        }

    def test_the_human_report_names_every_affected_resource(
        self,
        past_present_future: FakeCalDAVServer,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Brief §8: one line per affected resource, with UID, summary, start, and action."""
        env_file = _write_env(tmp_path, past_present_future)

        main(
            [
                "delete",
                "before",
                "2025-01-01",
                *_argv(env_file, tmp_path / "backups", "--tz", "UTC"),
            ]
        )

        output = capsys.readouterr().out
        assert "past@arwen.test" in output
        assert "weekly@arwen.test" in output
        assert "straddling@arwen.test" in output
        assert "future@arwen.test" not in output
        assert "2024-01-01T09:00:00+00:00" in output
        assert "Weekly stand-up" in output

    def test_the_password_never_appears_in_verbose_output(
        self,
        past_present_future: FakeCalDAVServer,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Brief §3: the password is redacted from logs, including verbose HTTP ones."""
        env_file = _write_env(tmp_path, past_present_future)

        main(
            [
                "delete",
                "before",
                "2025-01-01",
                *_argv(env_file, tmp_path / "backups", "--verbose", "--tz", "UTC"),
            ]
        )

        captured = capsys.readouterr()
        assert "s3cret" not in captured.out
        assert "s3cret" not in captured.err


class TestExitCodes:
    """Brief §7's exit codes, exercised through :func:`arwen.cli.main`."""

    def test_a_clean_run_exits_zero(self, tmp_path: Path) -> None:
        """Nothing to do, nothing skipped: exit 0."""
        collection = FakeCollection(name="personal", display_name="Personal")
        collection.add(
            FakeResource(
                name="future.ics",
                ics=_event("future@arwen.test", "Future", "20270101T090000Z", "20270101T100000Z"),
            )
        )
        server = FakeCalDAVServer(collections=[collection])
        with server:
            env_file = _write_env(tmp_path, server)

            code = main(["delete", "before", "2025-01-01", *_argv(env_file, tmp_path / "backups")])

            assert code == EXIT_OK

    def test_a_date_with_a_time_component_is_a_usage_error(
        self, past_present_future: FakeCalDAVServer, tmp_path: Path
    ) -> None:
        """Brief §2: ``DATE`` is ``YYYY-MM-DD``; no time component is accepted."""
        env_file = _write_env(tmp_path, past_present_future)

        code = main(
            ["delete", "before", "2025-01-01T00:00", *_argv(env_file, tmp_path / "backups")]
        )

        assert code == EXIT_USAGE

    def test_a_non_iso_date_is_a_usage_error(
        self, past_present_future: FakeCalDAVServer, tmp_path: Path
    ) -> None:
        """A compact ``YYYYMMDD`` date is rejected rather than silently accepted."""
        env_file = _write_env(tmp_path, past_present_future)

        code = main(["delete", "before", "20250101", *_argv(env_file, tmp_path / "backups")])

        assert code == EXIT_USAGE

    def test_an_unknown_timezone_is_a_usage_error(
        self, past_present_future: FakeCalDAVServer, tmp_path: Path
    ) -> None:
        """``--tz`` takes an IANA name; anything else is rejected before connecting."""
        env_file = _write_env(tmp_path, past_present_future)

        code = main(
            [
                "delete",
                "before",
                "2025-01-01",
                *_argv(env_file, tmp_path / "backups", "--tz", "Middle/Earth"),
            ]
        )

        assert code == EXIT_USAGE

    def test_a_missing_credentials_file_is_a_usage_error(self, tmp_path: Path) -> None:
        """An unreadable ``arwen.env`` is reported, never guessed around."""
        code = main(
            [
                "delete",
                "duplicates",
                "--env-file",
                str(tmp_path / "absent.env"),
                "--backup-dir",
                str(tmp_path / "backups"),
            ]
        )

        assert code == EXIT_USAGE

    def test_no_calendar_and_non_tty_stdin_is_a_usage_error(
        self, past_present_future: FakeCalDAVServer, tmp_path: Path
    ) -> None:
        """Brief §4: with no unambiguous ``--calendar`` and no TTY, refuse to guess."""
        env_file = _write_env(tmp_path, past_present_future)

        code = main(
            [
                "delete",
                "duplicates",
                "--env-file",
                str(env_file),
                "--backup-dir",
                str(tmp_path / "backups"),
            ]
        )

        assert code == EXIT_USAGE

    def test_an_unreachable_server_is_a_connection_failure(self, tmp_path: Path) -> None:
        """A server that is not listening yields exit 3, not a traceback."""
        server = FakeCalDAVServer(collections=[FakeCollection(name="personal", display_name="P")])
        server.start()
        base_url = server.base_url
        server.stop()
        env_file = tmp_path / "arwen.env"
        env_file.write_text(
            f"ARWEN_CALDAV_URL={base_url}\n"
            "ARWEN_CALDAV_USERNAME=user\n"
            "ARWEN_CALDAV_PASSWORD=pass\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)

        code = main(["delete", "duplicates", *_argv(env_file, tmp_path / "backups")])

        assert code == EXIT_CONNECTION
