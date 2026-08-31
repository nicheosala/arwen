"""Tests for the in-process fake CalDAV server itself.

This is test infrastructure that everything else will depend on, so it must
be trustworthy on its own before any client code is built against it: ETag
mutation on PUT, the 412 conflict path (both organic and forced), the request
log, and determinism of the shuffled-ordering mode are all exercised here
directly over HTTP, using nothing but the standard library.
"""

import datetime
import http.client
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from tests.fake_server import (
    CALDAV_NS,
    DAV_NS,
    FakeCalDAVServer,
    FakeCollection,
    FakeResource,
)

_ICAL_UTC_FORMAT = "%Y%m%dT%H%M%SZ"


def _ics(uid: str, summary: str, dtstart: str = "20260101T090000Z") -> bytes:
    """Build a minimal single-VEVENT ``.ics`` document, one hour long."""
    start = datetime.datetime.strptime(dtstart, _ICAL_UTC_FORMAT).replace(tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)
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


def _request(
    base_url: str,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    """Issue a raw HTTP request against a running fake server and return (status, headers, body)."""
    host_port = base_url.removeprefix("http://")
    conn = http.client.HTTPConnection(host_port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        response_body = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, response_headers, response_body
    finally:
        conn.close()


def _hrefs(multistatus_body: bytes) -> list[str]:
    """Extract ``D:href`` values, in document order, from a multistatus response."""
    root = ET.fromstring(multistatus_body)
    return [
        href_el.text or "" for href_el in root.findall(f".//{{{DAV_NS}}}response/{{{DAV_NS}}}href")
    ]


@pytest.fixture
def single_resource_server() -> Iterator[FakeCalDAVServer]:
    """A server with one collection holding one resource."""
    collection = FakeCollection(name="personal", display_name="Personal")
    collection.add(FakeResource(name="event1.ics", ics=_ics("uid-1", "Meeting")))
    server = FakeCalDAVServer(collections=[collection])
    with server:
        yield server


def _resource_href(server: FakeCalDAVServer) -> str:
    collection = server.collections["personal"]
    resource = collection.resources["event1.ics"]
    return server.resource_href(collection, resource)


class TestETagAndIfMatch:
    """ETag mutation on PUT and If-Match / 412 conflict semantics."""

    def test_etag_changes_on_successful_put(self, single_resource_server: FakeCalDAVServer) -> None:
        """A successful PUT must mint a new ETag, distinct from the previous one."""
        server = single_resource_server
        collection = server.collections["personal"]
        resource = collection.resources["event1.ics"]
        original_etag = resource.etag
        href = _resource_href(server)

        status, headers, _body = _request(
            server.base_url,
            "PUT",
            href,
            headers={"If-Match": original_etag},
            body=_ics("uid-1", "Meeting (rescheduled)"),
        )

        assert status == 204
        assert resource.etag != original_etag
        assert headers["etag"] == resource.etag

    def test_etag_changes_even_when_content_is_identical(
        self, single_resource_server: FakeCalDAVServer
    ) -> None:
        """Re-PUTting byte-identical content still mints a new ETag."""
        server = single_resource_server
        collection = server.collections["personal"]
        resource = collection.resources["event1.ics"]
        original_etag = resource.etag
        href = _resource_href(server)

        status, _headers, _body = _request(
            server.base_url,
            "PUT",
            href,
            headers={"If-Match": original_etag},
            body=resource.ics,
        )

        assert status == 204
        assert resource.etag != original_etag

    def test_put_with_stale_if_match_returns_412_and_leaves_resource_untouched(
        self, single_resource_server: FakeCalDAVServer
    ) -> None:
        """A stale If-Match must be rejected with 412 and must not mutate the resource."""
        server = single_resource_server
        collection = server.collections["personal"]
        resource = collection.resources["event1.ics"]
        original_etag = resource.etag
        original_ics = resource.ics
        href = _resource_href(server)

        status, _headers, _body = _request(
            server.base_url,
            "PUT",
            href,
            headers={"If-Match": '"not-the-current-etag"'},
            body=_ics("uid-1", "Should not apply"),
        )

        assert status == 412
        assert resource.etag == original_etag
        assert resource.ics == original_ics

    def test_delete_with_stale_if_match_returns_412_and_leaves_resource_in_place(
        self, single_resource_server: FakeCalDAVServer
    ) -> None:
        """A DELETE carrying a genuinely superseded ETag must be rejected with 412."""
        server = single_resource_server
        resource = server.collections["personal"].resources["event1.ics"]
        href = _resource_href(server)
        stale_etag = resource.etag

        put_status, _headers, _body = _request(
            server.base_url,
            "PUT",
            href,
            headers={"If-Match": stale_etag},
            body=_ics("uid-1", "Rescheduled"),
        )
        assert put_status == 204
        current_etag = resource.etag
        assert current_etag != stale_etag  # the PUT must have actually superseded it

        stale_status, _headers, _body = _request(
            server.base_url, "DELETE", href, headers={"If-Match": stale_etag}
        )

        assert stale_status == 412
        assert "event1.ics" in server.collections["personal"].resources
        assert resource.etag == current_etag

        # The comparison must not just reject the stale tag — it must also accept the
        # genuinely current one, or a mutant that rejects unconditionally would still
        # satisfy every assertion above.
        current_status, _headers, _body = _request(
            server.base_url, "DELETE", href, headers={"If-Match": current_etag}
        )

        assert current_status == 204
        assert "event1.ics" not in server.collections["personal"].resources

    def test_delete_with_matching_if_match_succeeds(
        self, single_resource_server: FakeCalDAVServer
    ) -> None:
        """A DELETE whose If-Match matches the current ETag must remove the resource."""
        server = single_resource_server
        resource = server.collections["personal"].resources["event1.ics"]
        href = _resource_href(server)

        status, _headers, _body = _request(
            server.base_url, "DELETE", href, headers={"If-Match": resource.etag}
        )

        assert status == 204
        assert "event1.ics" not in server.collections["personal"].resources

    def test_force_412_on_put_mode_rejects_even_a_correct_if_match(self) -> None:
        """In force-412 mode, PUT is rejected regardless of a correct If-Match."""
        collection = FakeCollection(name="personal", display_name="Personal")
        collection.add(FakeResource(name="event1.ics", ics=_ics("uid-1", "Meeting")))
        server = FakeCalDAVServer(collections=[collection], force_412_on_put=True)
        with server:
            resource = server.collections["personal"].resources["event1.ics"]
            href = _resource_href(server)

            status, _headers, _body = _request(
                server.base_url,
                "PUT",
                href,
                headers={"If-Match": resource.etag},
                body=_ics("uid-1", "Attempted update"),
            )

            assert status == 412
            assert resource.ics == _ics("uid-1", "Meeting")


class TestRequestLog:
    """Request recording, for asserting on what was actually sent over the wire."""

    def test_log_records_method_path_and_headers(
        self, single_resource_server: FakeCalDAVServer
    ) -> None:
        """The request log must capture method, path and headers (case-insensitively)."""
        server = single_resource_server
        resource = server.collections["personal"].resources["event1.ics"]
        href = _resource_href(server)

        _request(
            server.base_url,
            "DELETE",
            href,
            headers={"If-Match": resource.etag, "Schedule-Reply": "F"},
        )

        assert len(server.requests) == 1
        recorded = server.requests[0]
        assert recorded.method == "DELETE"
        assert recorded.path == href
        assert recorded.headers["if-match"] == resource.etag
        assert recorded.headers["schedule-reply"] == "F"

    def test_log_records_request_body(self, single_resource_server: FakeCalDAVServer) -> None:
        """The request log must capture the raw request body, e.g. a PUT payload."""
        server = single_resource_server
        resource = server.collections["personal"].resources["event1.ics"]
        href = _resource_href(server)
        new_body = _ics("uid-1", "Updated")

        _request(server.base_url, "PUT", href, headers={"If-Match": resource.etag}, body=new_body)

        assert len(server.requests) == 1
        assert server.requests[0].body == new_body

    def test_log_accumulates_across_multiple_requests_in_order(
        self, single_resource_server: FakeCalDAVServer
    ) -> None:
        """Multiple requests must be recorded in the order they were issued."""
        server = single_resource_server
        href = _resource_href(server)

        _request(server.base_url, "PROPFIND", href, headers={"Depth": "0"})
        _request(server.base_url, "PROPFIND", href, headers={"Depth": "0"})

        assert [r.method for r in server.requests] == ["PROPFIND", "PROPFIND"]


class TestShuffledOrdering:
    """Seeded-shuffle mode: reproducible ordering that differs from insertion order."""

    @staticmethod
    def _build_collection(name: str, count: int) -> FakeCollection:
        collection = FakeCollection(name=name, display_name=name)
        for i in range(count):
            collection.add(FakeResource(name=f"event{i}.ics", ics=_ics(f"uid-{i}", f"Event {i}")))
        return collection

    def test_same_seed_yields_identical_resource_order_across_server_instances(self) -> None:
        """Two independently-built servers with the same seed must list resources identically."""
        collection_a = self._build_collection("personal", 8)
        collection_b = self._build_collection("personal", 8)

        server_a = FakeCalDAVServer(collections=[collection_a], shuffle_seed=42)
        server_b = FakeCalDAVServer(collections=[collection_b], shuffle_seed=42)

        with server_a, server_b:
            href_a = server_a.collection_href(collection_a)
            href_b = server_b.collection_href(collection_b)
            _status_a, _h_a, body_a = _request(
                server_a.base_url, "PROPFIND", href_a, headers={"Depth": "1"}
            )
            _status_b, _h_b, body_b = _request(
                server_b.base_url, "PROPFIND", href_b, headers={"Depth": "1"}
            )

        # First entry in each multistatus is the collection itself; compare the rest.
        assert _hrefs(body_a)[1:] == _hrefs(body_b)[1:]

    def test_shuffled_order_differs_from_insertion_order(self) -> None:
        """With a seed set, resource order must not be insertion order (for a large enough set)."""
        collection = self._build_collection("personal", 8)
        insertion_order = list(collection.resources.keys())
        server = FakeCalDAVServer(collections=[collection], shuffle_seed=1234)

        with server:
            href = server.collection_href(collection)
            _status, _headers, body = _request(
                server.base_url, "PROPFIND", href, headers={"Depth": "1"}
            )

        shuffled_names = [h.rsplit("/", 1)[-1] for h in _hrefs(body)[1:]]
        assert shuffled_names != insertion_order
        assert sorted(shuffled_names) == sorted(insertion_order)

    def test_same_seed_yields_identical_collection_order(self) -> None:
        """Collection ordering under the home set must also be deterministic for a given seed."""
        collections_a = [FakeCollection(name=f"cal{i}", display_name=f"Cal {i}") for i in range(6)]
        collections_b = [FakeCollection(name=f"cal{i}", display_name=f"Cal {i}") for i in range(6)]

        server_a = FakeCalDAVServer(collections=collections_a, shuffle_seed=7)
        server_b = FakeCalDAVServer(collections=collections_b, shuffle_seed=7)

        with server_a, server_b:
            _status_a, _h_a, body_a = _request(
                server_a.base_url, "PROPFIND", server_a.home_path, headers={"Depth": "1"}
            )
            _status_b, _h_b, body_b = _request(
                server_b.base_url, "PROPFIND", server_b.home_path, headers={"Depth": "1"}
            )

        assert _hrefs(body_a)[1:] == _hrefs(body_b)[1:]

    def test_no_seed_preserves_insertion_order(self) -> None:
        """Without a seed, resources must be listed in insertion order."""
        collection = self._build_collection("personal", 5)
        server = FakeCalDAVServer(collections=[collection])

        with server:
            href = server.collection_href(collection)
            _status, _headers, body = _request(
                server.base_url, "PROPFIND", href, headers={"Depth": "1"}
            )

        names = [h.rsplit("/", 1)[-1] for h in _hrefs(body)[1:]]
        assert names == list(collection.resources.keys())


class TestCapabilityDiscovery:
    """OPTIONS / supported-report-set advertisement, including the no-calendar-query mode."""

    def test_options_advertises_calendar_access(
        self, single_resource_server: FakeCalDAVServer
    ) -> None:
        """OPTIONS must advertise calendar-access in the DAV header."""
        server = single_resource_server
        status, headers, _body = _request(server.base_url, "OPTIONS", "/")
        assert status == 200
        assert "calendar-access" in headers["dav"]

    def test_supported_report_set_includes_calendar_query_by_default(
        self, single_resource_server: FakeCalDAVServer
    ) -> None:
        """By default, PROPFIND on a collection must advertise calendar-query support."""
        server = single_resource_server
        href = server.collection_href(server.collections["personal"])
        status, _headers, body = _request(server.base_url, "PROPFIND", href, headers={"Depth": "0"})
        assert status == 207
        root = ET.fromstring(body)
        query_els = root.findall(f".//{{{CALDAV_NS}}}calendar-query")
        assert len(query_els) == 1

    def test_no_calendar_query_mode_omits_it_from_supported_report_set(self) -> None:
        """With advertise_calendar_query off, calendar-query must be absent from the report set."""
        collection = FakeCollection(name="personal", display_name="Personal")
        server = FakeCalDAVServer(collections=[collection], advertise_calendar_query=False)
        with server:
            href = server.collection_href(collection)
            _status, _headers, body = _request(
                server.base_url, "PROPFIND", href, headers={"Depth": "0"}
            )

        root = ET.fromstring(body)
        query_els = root.findall(f".//{{{CALDAV_NS}}}calendar-query")
        assert query_els == []

    def test_no_calendar_query_mode_rejects_report_with_403(self) -> None:
        """With advertise_calendar_query=False, a REPORT request must be rejected outright."""
        collection = FakeCollection(name="personal", display_name="Personal")
        collection.add(FakeResource(name="event1.ics", ics=_ics("uid-1", "Meeting")))
        server = FakeCalDAVServer(collections=[collection], advertise_calendar_query=False)
        with server:
            href = server.collection_href(collection)
            status, _headers, _body = _request(server.base_url, "REPORT", href)

        assert status == 403


class TestPropfindDiscoveryFlow:
    """End-to-end principal -> calendar-home-set -> collection discovery."""

    def test_full_discovery_chain(self, single_resource_server: FakeCalDAVServer) -> None:
        """PROPFIND on '/' then the principal then the home set must chain correctly."""
        server = single_resource_server

        status, _headers, body = _request(server.base_url, "PROPFIND", "/", headers={"Depth": "0"})
        assert status == 207
        root = ET.fromstring(body)
        principal_href = root.findtext(f".//{{{DAV_NS}}}current-user-principal/{{{DAV_NS}}}href")
        assert principal_href == server.principal_path

        status, _headers, body = _request(
            server.base_url, "PROPFIND", principal_href, headers={"Depth": "0"}
        )
        assert status == 207
        root = ET.fromstring(body)
        home_href = root.findtext(f".//{{{CALDAV_NS}}}calendar-home-set/{{{DAV_NS}}}href")
        assert home_href == server.home_path

        status, _headers, body = _request(
            server.base_url, "PROPFIND", home_href, headers={"Depth": "1"}
        )
        assert status == 207
        assert server.collection_href(server.collections["personal"]) in _hrefs(body)


class TestCalendarQueryTimeRange:
    """REPORT calendar-query with a time-range filter."""

    def test_time_range_filters_out_events_outside_the_window(self) -> None:
        """Only the event whose occurrence overlaps the requested time-range must be returned."""
        collection = FakeCollection(name="personal", display_name="Personal")
        past_ics = _ics("uid-past", "Old", "20200101T090000Z")
        collection.add(FakeResource(name="past.ics", ics=past_ics))
        collection.add(
            FakeResource(name="inside.ics", ics=_ics("uid-inside", "Current", "20260601T090000Z"))
        )
        server = FakeCalDAVServer(collections=[collection])
        with server:
            href = server.collection_href(collection)
            report_body = (
                b'<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
                b"<D:prop><D:getetag/></D:prop>"
                b'<C:filter><C:comp-filter name="VCALENDAR">'
                b'<C:comp-filter name="VEVENT">'
                b'<C:time-range start="20260101T000000Z" end="20270101T000000Z"/>'
                b"</C:comp-filter></C:comp-filter></C:filter>"
                b"</C:calendar-query>"
            )
            status, _headers, body = _request(server.base_url, "REPORT", href, body=report_body)

        assert status == 207
        expected_href = server.resource_href(collection, collection.resources["inside.ics"])
        assert _hrefs(body) == [expected_href]
