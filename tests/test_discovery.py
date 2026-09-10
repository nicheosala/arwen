"""Integration tests for brief §4: calendar discovery and the interactive picker.

Against the in-process fake server, exercising the full discovery chain
(:func:`arwen.discovery.discover_calendars`) and every branch of
:func:`arwen.discovery.select_calendar`: unambiguous ``--calendar``
preselection, ambiguous/absent preselection falling back to the prompt, the
non-TTY usage-error path, and the numbered interactive picker itself.
"""

import io
from typing import TYPE_CHECKING, override

import pytest

from arwen.config import Credentials
from arwen.dav import DavConnection
from arwen.discovery import CalendarSelectionError, discover_calendars, select_calendar
from tests.fake_server import FakeCalDAVServer, FakeCollection, FakeResource

if TYPE_CHECKING:
    from collections.abc import Iterator

_MINIMAL_ICS = (
    b"BEGIN:VCALENDAR\r\n"
    b"VERSION:2.0\r\n"
    b"PRODID:-//arwen tests//EN\r\n"
    b"BEGIN:VEVENT\r\n"
    b"UID:uid-1\r\n"
    b"SUMMARY:Event\r\n"
    b"DTSTART:20250101T090000Z\r\n"
    b"DTEND:20250101T100000Z\r\n"
    b"END:VEVENT\r\n"
    b"END:VCALENDAR\r\n"
)


class _FakeTTY(io.StringIO):
    """A ``StringIO`` that reports itself as a TTY, for exercising the interactive path."""

    @override
    def isatty(self) -> bool:
        """Report as connected to a terminal, unlike a plain ``StringIO``."""
        return True


def _connection(server: FakeCalDAVServer) -> DavConnection:
    """Build a DavConnection pointed at a running fake server."""
    credentials = Credentials(url=server.base_url, username="user", password="pass")
    return DavConnection(credentials, timeout=5)


@pytest.fixture
def mixed_component_server() -> Iterator[FakeCalDAVServer]:
    """A server with one VEVENT calendar and one VTODO-only collection."""
    events = FakeCollection(name="personal", display_name="Personal")
    events.add(FakeResource(name="e.ics", ics=_MINIMAL_ICS))
    tasks = FakeCollection(name="tasks", display_name="Tasks", supported_components=("VTODO",))
    server = FakeCalDAVServer(collections=[events, tasks])
    with server:
        yield server


class TestDiscoverCalendars:
    """§4 step 2: only VEVENT-capable collections are offered."""

    def test_filters_out_non_vevent_collections(
        self, mixed_component_server: FakeCalDAVServer
    ) -> None:
        """A VTODO-only collection never appears in the discovered list."""
        connection = _connection(mixed_component_server)

        calendars = discover_calendars(connection)

        assert [c.display_name for c in calendars] == ["Personal"]

    def test_no_calendars_yields_empty_list(self) -> None:
        """A server with no calendar collections at all yields an empty list, not an error."""
        server = FakeCalDAVServer(collections=[])
        with server:
            connection = _connection(server)

            calendars = discover_calendars(connection)

            assert calendars == []


class TestSelectCalendarPreselect:
    """``--calendar NAME`` matching, per brief §4."""

    def test_unambiguous_preselect_skips_the_prompt(
        self, mixed_component_server: FakeCalDAVServer
    ) -> None:
        """A unique display-name match returns immediately without reading input."""
        connection = _connection(mixed_component_server)
        exploding_input = io.StringIO()  # empty: readline() would return "" if ever touched

        selected = select_calendar(
            connection,
            preselect="Personal",
            input_stream=exploding_input,
            output_stream=io.StringIO(),
        )

        assert selected.display_name == "Personal"

    def test_unmatched_preselect_falls_back_and_fails_on_non_tty(
        self, mixed_component_server: FakeCalDAVServer
    ) -> None:
        """A preselect matching nothing falls back to the prompt, which fails on non-TTY input."""
        connection = _connection(mixed_component_server)

        with pytest.raises(CalendarSelectionError):
            select_calendar(
                connection,
                preselect="No Such Calendar",
                input_stream=io.StringIO(),
                output_stream=io.StringIO(),
            )

    def test_ambiguous_preselect_falls_back_and_fails_on_non_tty(self) -> None:
        """Two calendars sharing a display name make --calendar ambiguous, so it falls back."""
        first = FakeCollection(name="a", display_name="Shared")
        first.add(FakeResource(name="e.ics", ics=_MINIMAL_ICS))
        second = FakeCollection(name="b", display_name="Shared")
        second.add(FakeResource(name="e.ics", ics=_MINIMAL_ICS))
        server = FakeCalDAVServer(collections=[first, second])
        with server:
            connection = _connection(server)

            with pytest.raises(CalendarSelectionError):
                select_calendar(
                    connection,
                    preselect="Shared",
                    input_stream=io.StringIO(),
                    output_stream=io.StringIO(),
                )


class TestSelectCalendarNonInteractive:
    """Brief §4: non-TTY stdin with no unambiguous preselect is a usage error, never a guess."""

    def test_no_preselect_and_non_tty_raises(
        self, mixed_component_server: FakeCalDAVServer
    ) -> None:
        """With no --calendar and non-interactive stdin, selection refuses to guess."""
        connection = _connection(mixed_component_server)

        with pytest.raises(CalendarSelectionError):
            select_calendar(connection, input_stream=io.StringIO(), output_stream=io.StringIO())

    def test_no_calendars_available_raises_before_checking_tty(self) -> None:
        """An empty calendar list is reported on its own, even with a TTY-like input."""
        server = FakeCalDAVServer(collections=[])
        with server:
            connection = _connection(server)

            with pytest.raises(CalendarSelectionError, match="no calendars"):
                select_calendar(connection, input_stream=_FakeTTY(), output_stream=io.StringIO())


class TestSelectCalendarInteractive:
    """The numbered picker (brief §4 step 3), exercised with a simulated TTY."""

    def test_prints_numbered_list_and_reads_valid_selection(self) -> None:
        """The picker lists every calendar once, and a valid index selects it."""
        first = FakeCollection(name="a", display_name="Work")
        first.add(FakeResource(name="e.ics", ics=_MINIMAL_ICS))
        second = FakeCollection(name="b", display_name="Home")
        second.add(FakeResource(name="e.ics", ics=_MINIMAL_ICS))
        server = FakeCalDAVServer(collections=[first, second])
        with server:
            connection = _connection(server)
            calendars = discover_calendars(connection)
            output = io.StringIO()

            selected = select_calendar(
                connection, input_stream=_FakeTTY("1\n"), output_stream=output
            )

            printed = output.getvalue()
            for calendar in calendars:
                assert calendar.display_name in printed
                assert calendar.href in printed
            assert selected.display_name == calendars[0].display_name
            assert selected.href == calendars[0].href

    def test_out_of_range_selection_raises(self, mixed_component_server: FakeCalDAVServer) -> None:
        """A numeric selection outside the listed range is rejected."""
        connection = _connection(mixed_component_server)

        with pytest.raises(CalendarSelectionError, match="out of range"):
            select_calendar(connection, input_stream=_FakeTTY("99\n"), output_stream=io.StringIO())

    def test_non_numeric_selection_raises(self, mixed_component_server: FakeCalDAVServer) -> None:
        """A non-numeric selection is rejected with a clear error, not a crash."""
        connection = _connection(mixed_component_server)

        with pytest.raises(CalendarSelectionError, match="invalid selection"):
            select_calendar(
                connection, input_stream=_FakeTTY("not-a-number\n"), output_stream=io.StringIO()
            )

    def test_closed_input_raises(self, mixed_component_server: FakeCalDAVServer) -> None:
        """An empty read (input closed before a selection is given) is rejected."""
        connection = _connection(mixed_component_server)

        with pytest.raises(CalendarSelectionError, match="no selection"):
            select_calendar(connection, input_stream=_FakeTTY(""), output_stream=io.StringIO())
