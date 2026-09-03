"""Unit tests for brief §7's pre-mutation backup writer.

Layer 1 of brief §10: no server, no network. The assertions are on the bytes
that reach disk and on the failure mode that matters — a backup that cannot
be written must raise, so the caller can abort before touching the server.
"""

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar

from arwen.backup import (
    ETAG_PROPERTY,
    HREF_PROPERTY,
    BackupError,
    backup_path,
    render_backup,
    slugify,
    write_backup,
)
from arwen.dav import CalendarResource

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


def _resource(href: str, uid: str, summary: str = "Event", extra: str = "") -> CalendarResource:
    """Build a CalendarResource around a minimal single-``VEVENT`` document."""
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//arwen tests//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        "DTSTAMP:20240101T000000Z\r\n"
        f"SUMMARY:{summary}\r\n"
        "DTSTART:20240101T090000Z\r\n"
        "DTEND:20240101T100000Z\r\n"
        f"{extra}"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    parsed = Calendar.from_ical(ics)
    assert isinstance(parsed, Calendar)
    return CalendarResource(href=href, etag=f'"etag-{uid}"', calendar=parsed)


class TestSlugify:
    """The filename slug of brief §7's ``arwen-<calendar-slug>-<UTC-timestamp>.ics``."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Personal", "personal"),
            ("Work Calendar", "work-calendar"),
            ("  spaced  out  ", "spaced-out"),
            ("Family/Shared", "family-shared"),
            ("Kalender für Büro", "kalender-f-r-b-ro"),
            ("///", "calendar"),
            ("", "calendar"),
        ],
    )
    def test_slugs(self, name: str, expected: str) -> None:
        """A display name reduces to a filename-safe slug, never to an empty one."""
        assert slugify(name) == expected


class TestBackupPath:
    """The generated filename, per brief §7."""

    def test_name_carries_the_slug_and_a_utc_timestamp(self, tmp_path: Path) -> None:
        """The path is ``arwen-<slug>-<UTC timestamp>.ics`` inside the backup directory."""
        path = backup_path(tmp_path, "Work Calendar", now=_NOW)

        assert path.parent == tmp_path
        assert path.name == "arwen-work-calendar-20260304T050607Z.ics"

    def test_a_non_utc_instant_is_rendered_in_utc(self, tmp_path: Path) -> None:
        """The timestamp is always UTC, whatever zone the caller passed."""
        local = _NOW.astimezone(ZoneInfo("Pacific/Auckland"))

        assert (
            backup_path(tmp_path, "P", now=local).name == backup_path(tmp_path, "P", now=_NOW).name
        )


class TestRenderBackup:
    """The backup payload: one iCalendar stream, one document per resource."""

    def test_every_resource_becomes_its_own_document(self) -> None:
        """Two resources round-trip as two separate ``VCALENDAR`` documents."""
        payload = render_backup(
            [_resource("/cal/a.ics", "a@arwen.test"), _resource("/cal/b.ics", "b@arwen.test")]
        )

        documents = Calendar.from_ical(payload, multiple=True)
        assert len(documents) == 2
        assert {str(d.walk("VEVENT")[0]["UID"]) for d in documents} == {
            "a@arwen.test",
            "b@arwen.test",
        }

    def test_each_document_records_its_href_and_etag(self) -> None:
        """A future restore needs to know where each document came from, and at which ETag."""
        resource = _resource("/cal/a.ics", "a@arwen.test")

        (document,) = Calendar.from_ical(render_backup([resource]), multiple=True)

        assert str(document[HREF_PROPERTY]) == "/cal/a.ics"
        assert str(document[ETAG_PROPERTY]) == '"etag-a@arwen.test"'

    def test_the_callers_calendar_is_never_mutated(self) -> None:
        """Annotating the copy must not leave the annotation on the object being backed up."""
        resource = _resource("/cal/a.ics", "a@arwen.test")

        render_backup([resource])

        assert HREF_PROPERTY not in resource.calendar
        assert ETAG_PROPERTY not in resource.calendar

    def test_unknown_properties_and_non_ascii_survive_verbatim(self) -> None:
        """A backup that loses ``X-`` properties or accents is not a backup."""
        resource = _resource(
            "/cal/a.ics", "a@arwen.test", summary="Café — déjeuner", extra="X-KEEPSAKE:keep me\r\n"
        )

        (document,) = Calendar.from_ical(render_backup([resource]), multiple=True)

        event = document.walk("VEVENT")[0]
        assert str(event["SUMMARY"]) == "Café — déjeuner"
        assert str(event["X-KEEPSAKE"]) == "keep me"

    def test_no_resources_render_to_no_bytes(self) -> None:
        """An empty selection renders to an empty payload rather than a malformed one."""
        assert render_backup([]) == b""


class TestWriteBackup:
    """Brief §7: written, flushed, and fsynced before the caller may mutate anything."""

    def test_creates_the_directory_and_writes_the_payload(self, tmp_path: Path) -> None:
        """A missing ``--backup-dir`` is created, and holds exactly the rendered payload."""
        resources = [_resource("/cal/a.ics", "a@arwen.test")]
        directory = tmp_path / "backups" / "nested"

        path = write_backup(directory, "Personal", resources, now=_NOW)

        assert path.parent == directory
        assert path.read_bytes() == render_backup(resources)

    def test_a_directory_that_cannot_be_created_raises(self, tmp_path: Path) -> None:
        """A regular file where the backup directory should be aborts with BackupError."""
        blocked = tmp_path / "blocked"
        blocked.write_text("occupied", encoding="utf-8")

        with pytest.raises(BackupError):
            write_backup(blocked, "Personal", [_resource("/cal/a.ics", "a@arwen.test")], now=_NOW)

        assert blocked.read_text(encoding="utf-8") == "occupied"

    def test_a_file_that_cannot_be_opened_raises(self, tmp_path: Path) -> None:
        """A read-only backup directory aborts with BackupError rather than a bare OSError."""
        if os.geteuid() == 0:
            pytest.skip("root bypasses directory permissions")
        directory = tmp_path / "readonly"
        directory.mkdir()
        directory.chmod(0o500)

        try:
            with pytest.raises(BackupError):
                write_backup(
                    directory, "Personal", [_resource("/cal/a.ics", "a@arwen.test")], now=_NOW
                )
        finally:
            directory.chmod(0o700)

    def test_the_written_file_is_reparseable(self, tmp_path: Path) -> None:
        """What lands on disk parses back into the documents that went in."""
        resources = [_resource(f"/cal/{index}.ics", f"{index}@arwen.test") for index in range(3)]

        path = write_backup(tmp_path / "backups", "Personal", resources, now=_NOW)

        documents = Calendar.from_ical(path.read_bytes(), multiple=True)
        assert [str(d[HREF_PROPERTY]) for d in documents] == [r.href for r in resources]
