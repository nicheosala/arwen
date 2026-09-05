"""Integration tests for the brief §4 DAV layer, against the in-process fake server.

Covers principal/calendar-home-set discovery, capability discovery via
``OPTIONS`` and ``supported-report-set``, the server-side/client-side
``time-range`` listing equivalence brief §4 requires a test for, and the
``If-Match``/``Schedule-Reply: F`` invariants of every mutation (brief §7 /
CLAUDE.md), asserted directly against the fake server's recorded requests —
never against internal call counts.
"""

import datetime
from typing import TYPE_CHECKING

import pytest
from icalendar import Calendar

from arwen.config import Credentials
from arwen.dav import (
    CalendarResource,
    Capabilities,
    DavConnection,
    DavRequestError,
    PreconditionFailedError,
    TimeRange,
    _parse_multistatus,
)
from tests.fake_server import (
    FakeCalDAVServer,
    FakeCollection,
    FakeResource,
    PropfindCalendarData,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_ICAL_UTC_FORMAT = "%Y%m%dT%H%M%SZ"


def _ics(uid: str, summary: str, dtstart: str, duration_hours: int = 1) -> bytes:
    """Build a minimal single-``VEVENT`` ``.ics`` document."""
    start = datetime.datetime.strptime(dtstart, _ICAL_UTC_FORMAT).replace(tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=duration_hours)
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//arwen tests//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DTSTART:{dtstart}\r\n"
        f"DTEND:{end.strftime(_ICAL_UTC_FORMAT)}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode()


def _connection(server: FakeCalDAVServer) -> DavConnection:
    """Build a DavConnection pointed at a running fake server."""
    credentials = Credentials(url=server.base_url, username="user", password="pass")
    return DavConnection(credentials, timeout=5)


@pytest.fixture
def three_event_server() -> Iterator[FakeCalDAVServer]:
    """A server with one calendar holding three events spread across two years."""
    collection = FakeCollection(name="personal", display_name="Personal")
    collection.add(
        FakeResource(name="past1.ics", ics=_ics("uid-past-1", "Past One", "20240101T090000Z"))
    )
    collection.add(
        FakeResource(name="past2.ics", ics=_ics("uid-past-2", "Past Two", "20240601T090000Z"))
    )
    collection.add(
        FakeResource(name="future.ics", ics=_ics("uid-future", "Future", "20270101T090000Z"))
    )
    server = FakeCalDAVServer(collections=[collection])
    with server:
        yield server


class TestDiscovery:
    """Principal, calendar-home-set, and collection listing (brief §4 step 1-2)."""

    def test_discover_principal(self, three_event_server: FakeCalDAVServer) -> None:
        """The principal href discovered matches the server's configured principal path."""
        connection = _connection(three_event_server)

        principal = connection.discover_principal()

        assert principal == three_event_server.principal_path

    def test_discover_calendar_home_set(self, three_event_server: FakeCalDAVServer) -> None:
        """The home-set href discovered matches the server's configured home path."""
        connection = _connection(three_event_server)

        principal = connection.discover_principal()
        home = connection.discover_calendar_home_set(principal)

        assert home == three_event_server.home_path

    def test_list_calendars_reports_display_name_and_components(
        self, three_event_server: FakeCalDAVServer
    ) -> None:
        """A listed collection carries its display name, href, and VEVENT support."""
        connection = _connection(three_event_server)
        home = connection.discover_calendar_home_set(connection.discover_principal())

        calendars = connection.list_calendars(home)

        assert len(calendars) == 1
        (calendar,) = calendars
        assert calendar.display_name == "Personal"
        assert calendar.href == three_event_server.collection_href(
            three_event_server.collections["personal"]
        )
        assert "VEVENT" in calendar.supported_components

    def test_list_calendars_excludes_non_calendar_collections(self) -> None:
        """A collection whose supported-calendar-component-set has no VEVENT is still listed.

        (brief §4 filters on VEVENT support in the *discovery orchestration*,
        not the raw collection listing — this asserts the raw listing
        returns every calendar collection regardless.)
        """
        todo_only = FakeCollection(
            name="tasks", display_name="Tasks", supported_components=("VTODO",)
        )
        server = FakeCalDAVServer(collections=[todo_only])
        with server:
            connection = _connection(server)
            home = connection.discover_calendar_home_set(connection.discover_principal())

            calendars = connection.list_calendars(home)

            assert len(calendars) == 1
            assert calendars[0].supported_components == frozenset({"VTODO"})


class TestCapabilityDiscovery:
    """OPTIONS DAV: header and supported-report-set, per brief §4."""

    def test_reports_calendar_query_when_advertised(
        self, three_event_server: FakeCalDAVServer
    ) -> None:
        """A server advertising calendar-query is reported as supporting it."""
        connection = _connection(three_event_server)
        home = connection.discover_calendar_home_set(connection.discover_principal())
        (calendar,) = connection.list_calendars(home)

        capabilities = connection.discover_capabilities(calendar.href)

        assert capabilities.calendar_access is True
        assert capabilities.supports_calendar_query is True

    def test_reports_no_calendar_query_when_not_advertised(self) -> None:
        """A server that omits calendar-query from supported-report-set is reported as such."""
        collection = FakeCollection(name="personal", display_name="Personal")
        collection.add(FakeResource(name="e.ics", ics=_ics("uid-1", "Event", "20250101T090000Z")))
        server = FakeCalDAVServer(collections=[collection], advertise_calendar_query=False)
        with server:
            connection = _connection(server)
            home = connection.discover_calendar_home_set(connection.discover_principal())
            (calendar,) = connection.list_calendars(home)

            capabilities = connection.discover_capabilities(calendar.href)

            assert capabilities.supports_calendar_query is False


class TestListResourcesEquivalence:
    """Brief §4: the server-side REPORT and client-side PROPFIND paths must agree."""

    def _list_both_ways(
        self, server: FakeCalDAVServer, time_range: TimeRange
    ) -> tuple[set[str], set[str]]:
        connection = _connection(server)
        home = connection.discover_calendar_home_set(connection.discover_principal())
        (calendar,) = connection.list_calendars(home)

        server_side = Capabilities(calendar_access=True, supports_calendar_query=True)
        client_side = Capabilities(calendar_access=True, supports_calendar_query=False)

        server_hrefs = {
            r.href for r in connection.list_resources(calendar.href, server_side, time_range)
        }
        client_hrefs = {
            r.href for r in connection.list_resources(calendar.href, client_side, time_range)
        }
        return server_hrefs, client_hrefs

    def test_both_paths_agree_on_a_before_boundary(
        self, three_event_server: FakeCalDAVServer
    ) -> None:
        """Querying everything before 2026-01-01 yields the same two past events either way."""
        time_range = TimeRange(end=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))

        server_hrefs, client_hrefs = self._list_both_ways(three_event_server, time_range)

        assert server_hrefs == client_hrefs
        assert len(server_hrefs) == 2
        assert all("past" in href for href in server_hrefs)

    def test_both_paths_agree_on_an_empty_result(
        self, three_event_server: FakeCalDAVServer
    ) -> None:
        """A boundary before every event yields an empty set on both paths."""
        time_range = TimeRange(end=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC))

        server_hrefs, client_hrefs = self._list_both_ways(three_event_server, time_range)

        assert server_hrefs == client_hrefs == set()

    def test_both_paths_agree_on_a_full_result(self, three_event_server: FakeCalDAVServer) -> None:
        """A boundary after every event yields all three resources on both paths."""
        time_range = TimeRange(end=datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC))

        server_hrefs, client_hrefs = self._list_both_ways(three_event_server, time_range)

        assert server_hrefs == client_hrefs
        assert len(server_hrefs) == 3

    def test_no_time_range_lists_everything_unfiltered(
        self, three_event_server: FakeCalDAVServer
    ) -> None:
        """With no time_range, every resource is returned regardless of capabilities."""
        connection = _connection(three_event_server)
        home = connection.discover_calendar_home_set(connection.discover_principal())
        (calendar,) = connection.list_calendars(home)
        capabilities = Capabilities(calendar_access=True, supports_calendar_query=True)

        resources = connection.list_resources(calendar.href, capabilities)

        assert len(resources) == 3

    def test_shuffled_server_order_does_not_change_the_result_set(self) -> None:
        """A shuffled resource listing order still yields the same set of resources."""
        collection = FakeCollection(name="personal", display_name="Personal")
        for index in range(5):
            collection.add(
                FakeResource(
                    name=f"e{index}.ics",
                    ics=_ics(f"uid-{index}", f"Event {index}", "20240101T090000Z"),
                )
            )
        server = FakeCalDAVServer(collections=[collection], shuffle_seed=42)
        with server:
            connection = _connection(server)
            home = connection.discover_calendar_home_set(connection.discover_principal())
            (calendar,) = connection.list_calendars(home)
            capabilities = Capabilities(calendar_access=True, supports_calendar_query=True)

            resources = connection.list_resources(calendar.href, capabilities)

            assert {r.href for r in resources} == {
                server.resource_href(collection, r) for r in collection.resources.values()
            }


class TestMutations:
    """If-Match, Schedule-Reply: F, and conflict handling on PUT/DELETE (brief §7)."""

    def _single_resource(self) -> tuple[FakeCalDAVServer, FakeCollection, FakeResource]:
        collection = FakeCollection(name="personal", display_name="Personal")
        resource = FakeResource(name="e.ics", ics=_ics("uid-1", "Event", "20250101T090000Z"))
        collection.add(resource)
        server = FakeCalDAVServer(collections=[collection])
        return server, collection, resource

    def test_put_succeeds_and_returns_new_etag(self) -> None:
        """A PUT with a matching If-Match succeeds and returns a fresh ETag."""
        server, collection, resource = self._single_resource()
        with server:
            connection = _connection(server)
            href = server.resource_href(collection, resource)
            original_etag = resource.etag
            calendar = Calendar.from_ical(resource.ics)
            calendar.walk("VEVENT")[0]["SUMMARY"] = "Updated"

            new_etag = connection.put_resource(href, calendar, if_match=original_etag)

            assert new_etag != original_etag
            assert resource.etag == new_etag
            assert b"Updated" in resource.ics

    def test_put_and_delete_carry_if_match_and_schedule_reply(self) -> None:
        """Every PUT and DELETE the server observed carries both headers, brief §7 / CLAUDE.md."""
        server, collection, resource = self._single_resource()
        with server:
            connection = _connection(server)
            href = server.resource_href(collection, resource)
            calendar = Calendar.from_ical(resource.ics)

            etag = connection.put_resource(href, calendar, if_match=resource.etag)
            connection.delete_resource(href, if_match=etag)

            mutating = [r for r in server.requests if r.method in ("PUT", "DELETE")]
            assert len(mutating) == 2
            for request in mutating:
                assert request.headers.get("schedule-reply") == "F"
                assert request.headers.get("if-match") is not None

    def test_put_conflict_raises_precondition_failed(self) -> None:
        """A stale If-Match raises PreconditionFailedError and leaves the resource untouched."""
        server, collection, resource = self._single_resource()
        with server:
            connection = _connection(server)
            href = server.resource_href(collection, resource)
            calendar = Calendar.from_ical(resource.ics)
            original_content = resource.ics

            with pytest.raises(PreconditionFailedError):
                connection.put_resource(href, calendar, if_match='"stale-etag"')

            assert resource.ics == original_content

    def test_forced_412_on_put_raises_precondition_failed(self) -> None:
        """The fake server's force-412 mode is surfaced as PreconditionFailedError too."""
        collection = FakeCollection(name="personal", display_name="Personal")
        resource = FakeResource(name="e.ics", ics=_ics("uid-1", "Event", "20250101T090000Z"))
        collection.add(resource)
        server = FakeCalDAVServer(collections=[collection], force_412_on_put=True)
        with server:
            connection = _connection(server)
            href = server.resource_href(collection, resource)
            calendar = Calendar.from_ical(resource.ics)

            with pytest.raises(PreconditionFailedError):
                connection.put_resource(href, calendar, if_match=resource.etag)

    def test_delete_conflict_raises_precondition_failed(self) -> None:
        """A stale If-Match on DELETE raises PreconditionFailedError and keeps the resource."""
        server, collection, resource = self._single_resource()
        with server:
            connection = _connection(server)
            href = server.resource_href(collection, resource)

            with pytest.raises(PreconditionFailedError):
                connection.delete_resource(href, if_match='"stale-etag"')

            assert resource.name in collection.resources

    def test_delete_of_missing_resource_raises_dav_request_error(self) -> None:
        """Deleting a resource that no longer exists raises DavRequestError, not a silent no-op."""
        server, collection, _resource = self._single_resource()
        with server:
            connection = _connection(server)
            missing_href = server.collection_href(collection) + "does-not-exist.ics"

            with pytest.raises(DavRequestError):
                connection.delete_resource(missing_href, if_match='"whatever"')

    def test_dry_run_style_usage_issues_no_mutating_requests(
        self, three_event_server: FakeCalDAVServer
    ) -> None:
        """Read-only discovery and listing alone never issues a PUT or DELETE."""
        connection = _connection(three_event_server)
        home = connection.discover_calendar_home_set(connection.discover_principal())
        (calendar,) = connection.list_calendars(home)
        capabilities = connection.discover_capabilities(calendar.href)
        connection.list_resources(calendar.href, capabilities)

        mutating = [r for r in three_event_server.requests if r.method in ("PUT", "DELETE")]
        assert mutating == []


class TestCalendarDataFallback:
    """Listings must not assume ``PROPFIND`` returned ``calendar-data`` (RFC 4791 §9.6).

    ``calendar-data`` is a REPORT property. A server is free to answer
    ``PROPFIND`` without it, and Stalwart does: it names the property as
    unavailable with an empty ``<C:calendar-data/>`` under a ``404``
    propstat, for the collection's own entry and — in
    :attr:`~tests.fake_server.PropfindCalendarData.NOT_FOUND` — for its
    resources too. Handing that empty string to the iCalendar parser is what
    used to abort ``delete duplicates`` against a real server, so these
    tests drive the listing through every shape a conforming server may
    return.
    """

    def _server(
        self,
        mode: PropfindCalendarData,
        *,
        advertise_calendar_multiget: bool = True,
    ) -> FakeCalDAVServer:
        collection = FakeCollection(name="personal", display_name="Personal")
        collection.add(FakeResource(name="a.ics", ics=_ics("uid-a", "Event A", "20250101T090000Z")))
        collection.add(FakeResource(name="b.ics", ics=_ics("uid-b", "Event B", "20250601T090000Z")))
        collection.add(FakeResource(name="c.ics", ics=_ics("uid-c", "Event C", "20270101T090000Z")))
        return FakeCalDAVServer(
            collections=[collection],
            propfind_calendar_data=mode,
            advertise_calendar_multiget=advertise_calendar_multiget,
        )

    def _list(self, server: FakeCalDAVServer) -> list[CalendarResource]:
        connection = _connection(server)
        home = connection.discover_calendar_home_set(connection.discover_principal())
        (calendar,) = connection.list_calendars(home)
        capabilities = connection.discover_capabilities(calendar.href)
        return connection.list_resources(calendar.href, capabilities)

    @pytest.mark.parametrize(
        "mode",
        [PropfindCalendarData.INCLUDE, PropfindCalendarData.OMIT, PropfindCalendarData.NOT_FOUND],
    )
    def test_every_resource_is_listed_with_a_usable_body(self, mode: PropfindCalendarData) -> None:
        """Whatever PROPFIND does with calendar-data, listing yields three parsed events.

        The regression test for the ``delete duplicates`` crash: under
        ``NOT_FOUND`` the pre-fix client fed ``""`` to ``Calendar.from_ical``
        and raised ``ValueError: Found no components where exactly one is
        required``.
        """
        server = self._server(mode)
        with server:
            resources = self._list(server)

        assert len(resources) == 3
        summaries = {
            str(component.get("SUMMARY"))
            for resource in resources
            for component in resource.calendar.walk("VEVENT")
        }
        assert summaries == {"Event A", "Event B", "Event C"}

    @pytest.mark.parametrize("mode", [PropfindCalendarData.OMIT, PropfindCalendarData.NOT_FOUND])
    def test_missing_bodies_are_fetched_with_calendar_multiget(
        self, mode: PropfindCalendarData
    ) -> None:
        """One calendar-multiget REPORT supplies the bodies, rather than N GETs."""
        server = self._server(mode)
        with server:
            self._list(server)

        multigets = [
            request
            for request in server.requests
            if request.method == "REPORT" and b"calendar-multiget" in request.body
        ]
        assert len(multigets) == 1
        assert [r for r in server.requests if r.method == "GET"] == []

    @pytest.mark.parametrize("mode", [PropfindCalendarData.OMIT, PropfindCalendarData.NOT_FOUND])
    def test_falls_back_to_per_resource_get_without_multiget(
        self, mode: PropfindCalendarData
    ) -> None:
        """A server not advertising calendar-multiget is served by per-resource GETs."""
        server = self._server(mode, advertise_calendar_multiget=False)
        with server:
            resources = self._list(server)

        assert len(resources) == 3
        assert [r for r in server.requests if r.method == "REPORT"] == []
        assert sorted(r.path for r in server.requests if r.method == "GET") == [
            "/calendars/user/personal/a.ics",
            "/calendars/user/personal/b.ics",
            "/calendars/user/personal/c.ics",
        ]

    def test_no_extra_requests_when_propfind_supplied_the_bodies(self) -> None:
        """The fallback stays dormant when the listing already carried every body."""
        server = self._server(PropfindCalendarData.INCLUDE)
        with server:
            self._list(server)

        assert [r for r in server.requests if r.method == "GET"] == []
        assert [r for r in server.requests if r.method == "REPORT" and b"multiget" in r.body] == []

    @pytest.mark.parametrize(
        "mode",
        [PropfindCalendarData.INCLUDE, PropfindCalendarData.OMIT, PropfindCalendarData.NOT_FOUND],
    )
    def test_the_collection_self_entry_is_never_listed_as_a_resource(
        self, mode: PropfindCalendarData
    ) -> None:
        """A depth-1 listing describes the collection itself; that entry is not a resource.

        Under ``NOT_FOUND`` the self-entry carries a ``getetag`` and an empty
        ``calendar-data`` under a ``404`` propstat — indistinguishable from a
        resource to a client that reads properties without their status.
        """
        server = self._server(mode)
        with server:
            collection = server.collections["personal"]
            collection_href = server.collection_href(collection)
            resources = self._list(server)

        assert collection_href not in {resource.href for resource in resources}
        assert len(resources) == 3

    @pytest.mark.parametrize("mode", [PropfindCalendarData.OMIT, PropfindCalendarData.NOT_FOUND])
    def test_listed_etags_are_valid_for_a_later_mutation(self, mode: PropfindCalendarData) -> None:
        """A body fetched by the fallback comes back with an ETag If-Match accepts.

        Guards the fallback against returning a body paired with a stale or
        empty ETag, which would turn every mutation into a 412 (brief §7).
        """
        server = self._server(mode)
        with server:
            resources = self._list(server)
            connection = _connection(server)
            for resource in resources:
                connection.delete_resource(resource.href, if_match=resource.etag)

            assert server.collections["personal"].resources == {}

    def test_both_listing_paths_agree_when_propfind_omits_bodies(self) -> None:
        """Brief §4's equivalence still holds when only the REPORT path returns bodies."""
        time_range = TimeRange(end=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))
        server = self._server(PropfindCalendarData.NOT_FOUND)
        with server:
            connection = _connection(server)
            home = connection.discover_calendar_home_set(connection.discover_principal())
            (calendar,) = connection.list_calendars(home)

            server_side = Capabilities(
                calendar_access=True,
                supports_calendar_query=True,
                supports_calendar_multiget=True,
            )
            client_side = Capabilities(
                calendar_access=True,
                supports_calendar_query=False,
                supports_calendar_multiget=True,
            )

            server_hrefs = {
                r.href for r in connection.list_resources(calendar.href, server_side, time_range)
            }
            client_hrefs = {
                r.href for r in connection.list_resources(calendar.href, client_side, time_range)
            }

        assert server_hrefs == client_hrefs
        assert len(server_hrefs) == 2


class TestPropstatStatus:
    """A property is only supplied if its own propstat succeeded (RFC 4918 §13)."""

    def test_properties_of_a_non_2xx_propstat_are_not_visible(self) -> None:
        """A property named under a 404 propstat is absent, not present-and-empty."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            "<D:response><D:href>/c/e.ics</D:href>"
            '<D:propstat><D:prop><D:getetag>"abc"</D:getetag></D:prop>'
            "<D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
            "<D:propstat><D:prop><C:calendar-data/></D:prop>"
            "<D:status>HTTP/1.1 404 Not Found</D:status></D:propstat>"
            "</D:response></D:multistatus>"
        )

        (item,) = _parse_multistatus(body)

        assert set(item.props) == {"getetag"}

    def test_a_propstat_without_a_status_is_kept(self) -> None:
        """A malformed propstat carrying no status still yields its properties."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:multistatus xmlns:D="DAV:">'
            "<D:response><D:href>/c/e.ics</D:href>"
            '<D:propstat><D:prop><D:getetag>"abc"</D:getetag></D:prop></D:propstat>'
            "</D:response></D:multistatus>"
        )

        (item,) = _parse_multistatus(body)

        assert set(item.props) == {"getetag"}
