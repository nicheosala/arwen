"""In-process ``http.server``-based fake CalDAV server used by the test suite.

Implements just enough of RFC 4791 (CalDAV) and RFC 4918 (WebDAV) for the test
suite to exercise client code without a real network connection or server:
``PROPFIND``-based discovery of the principal, calendar-home-set, and calendar
collections; ``REPORT`` calendar-query with ``time-range`` filtering; and
``PUT`` / ``DELETE`` with real ``ETag`` and ``If-Match`` optimistic-concurrency
semantics.

Four behaviours are configurable at construction time:

- ``shuffle_seed``: when set, collections and resources are listed in a
  seeded-random order instead of insertion order, to prove that client-side
  logic (e.g. tie-breaks) is independent of server-returned ordering, while
  remaining reproducible across runs for the same seed.
- ``force_412_on_put``: every ``PUT`` is rejected with ``412 Precondition
  Failed`` regardless of ``If-Match``, to exercise conflict handling.
- ``advertise_calendar_query``: when ``False``, ``supported-report-set``
  omits ``calendar-query`` and ``REPORT`` requests are rejected with ``403``,
  forcing callers onto the client-side PROPFIND-and-filter fallback.
- Request recording is always on: every request handled is appended to
  ``FakeCalDAVServer.requests`` with method, path, headers, and body, so
  tests can assert on what was actually sent.

This module is test infrastructure only. It must not be imported by anything
under ``src/``.
"""

from __future__ import annotations

import datetime
import hashlib
import random
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import icalendar

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"


@dataclass
class FakeResource:
    """A single calendar resource (one ``.ics`` document) on the fake server.

    The ``etag`` is minted on creation and re-minted on every call to
    :meth:`replace`, so it is guaranteed to change on every successful
    ``PUT`` even when the content is byte-for-byte identical to before.
    """

    name: str
    ics: bytes
    etag: str = field(default="", init=False)
    _revision: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        """Mint the initial ETag."""
        self._bump_etag()

    def replace(self, ics: bytes) -> None:
        """Overwrite the resource content and mint a new ETag."""
        self.ics = ics
        self._bump_etag()

    def _bump_etag(self) -> None:
        self._revision += 1
        digest = hashlib.sha256(f"{self._revision}:".encode() + self.ics).hexdigest()[:16]
        self.etag = f'"{digest}"'


@dataclass
class FakeCollection:
    """A calendar collection: a named set of resources under the home set."""

    name: str
    display_name: str
    supported_components: tuple[str, ...] = ("VEVENT",)
    resources: dict[str, FakeResource] = field(default_factory=dict)

    def add(self, resource: FakeResource) -> None:
        """Add or replace a resource in this collection by name."""
        self.resources[resource.name] = resource


@dataclass(frozen=True)
class RecordedRequest:
    """One HTTP request observed by the fake server, for test assertions.

    ``headers`` keys are lower-cased for deterministic, case-insensitive
    lookup regardless of the casing the client sent them with.
    """

    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class _TimeRange:
    start: datetime.datetime | None
    end: datetime.datetime | None


class FakeCalDAVServer:
    """An in-process CalDAV server for the test suite.

    Runs a real ``http.server`` on a background thread bound to an ephemeral
    localhost port. Use as a context manager::

        with FakeCalDAVServer(collections=[...]) as server:
            ...  # server.base_url is now live
    """

    def __init__(
        self,
        collections: list[FakeCollection] | None = None,
        *,
        principal_path: str = "/principals/user/",
        home_path: str = "/calendars/user/",
        shuffle_seed: int | None = None,
        force_412_on_put: bool = False,
        advertise_calendar_query: bool = True,
    ) -> None:
        """Build a fake server. Call :meth:`start` (or use as a context manager) to serve."""
        self.collections: dict[str, FakeCollection] = {c.name: c for c in (collections or [])}
        self.principal_path = principal_path
        self.home_path = home_path
        self.shuffle_seed = shuffle_seed
        self.force_412_on_put = force_412_on_put
        self.advertise_calendar_query = advertise_calendar_query
        self.requests: list[RecordedRequest] = []
        self._requests_lock = threading.Lock()
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """The ``http://host:port`` root of the running server."""
        if self._httpd is None:
            raise RuntimeError("server is not running")
        host, port = self._httpd.server_address[:2]
        assert isinstance(host, str)
        return f"http://{host}:{port}"

    def start(self) -> None:
        """Start serving on a background thread, bound to an ephemeral port."""
        self._httpd = _Server(("127.0.0.1", 0), _Handler, self)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and join the background thread."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = None
        self._thread = None

    def __enter__(self) -> FakeCalDAVServer:
        """Start the server and return self."""
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Stop the server."""
        self.stop()

    def collection_href(self, collection: FakeCollection) -> str:
        """The absolute path of a collection."""
        return f"{self.home_path}{collection.name}/"

    def resource_href(self, collection: FakeCollection, resource: FakeResource) -> str:
        """The absolute path of a resource within a collection."""
        return f"{self.collection_href(collection)}{resource.name}"

    def find_collection(self, path: str) -> FakeCollection | None:
        """Look up a collection by its absolute path."""
        for collection in self.collections.values():
            if path == self.collection_href(collection):
                return collection
        return None

    def find_resource(self, path: str) -> tuple[FakeCollection, FakeResource] | None:
        """Look up a resource by its absolute path."""
        for collection in self.collections.values():
            href = self.collection_href(collection)
            if path.startswith(href):
                name = path[len(href) :]
                resource = collection.resources.get(name)
                if resource is not None:
                    return collection, resource
        return None

    def find_collection_for_resource_path(self, path: str) -> FakeCollection | None:
        """Find the collection a not-yet-existing resource path would belong to."""
        for collection in self.collections.values():
            href = self.collection_href(collection)
            remainder = path[len(href) :]
            if path.startswith(href) and remainder and "/" not in remainder:
                return collection
        return None

    def record_request(self, request: RecordedRequest) -> None:
        """Append a request to the log. Safe to call from any request-handling thread."""
        with self._requests_lock:
            self.requests.append(request)

    def ordered_collections(self) -> list[FakeCollection]:
        """Collections in insertion order, or seeded-shuffled order if configured."""
        items = list(self.collections.values())
        if self.shuffle_seed is not None:
            random.Random(f"{self.shuffle_seed}:collections").shuffle(items)
        return items

    def ordered_resources(self, collection: FakeCollection) -> list[FakeResource]:
        """A collection's resources in insertion order, or seeded-shuffled order."""
        items = list(collection.resources.values())
        if self.shuffle_seed is not None:
            random.Random(f"{self.shuffle_seed}:{collection.name}").shuffle(items)
        return items


class _Server(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        fake: FakeCalDAVServer,
    ) -> None:
        self.fake = fake
        super().__init__(address, handler_cls)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002, ARG002
        # Silence default request logging to stderr; tests read `requests` instead. The
        # `format`/unused-`args` signature must match BaseHTTPRequestHandler's exactly.
        return

    @property
    def fake(self) -> FakeCalDAVServer:
        server = self.server
        assert isinstance(server, _Server)
        return server.fake

    def _read_body(self) -> bytes:
        length_header = self.headers.get("Content-Length")
        length = int(length_header) if length_header else 0
        return self.rfile.read(length) if length else b""

    def _record(self, body: bytes) -> None:
        headers = {key.lower(): value for key, value in self.headers.items()}
        path = urlsplit(self.path).path
        self.fake.record_request(
            RecordedRequest(method=self.command, path=path, headers=headers, body=body)
        )

    def _send_empty(self, status: HTTPStatus, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_multistatus(self, responses: list[str]) -> None:
        body = _multistatus(responses)
        self.send_response(HTTPStatus.MULTI_STATUS)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        """Advertise CalDAV support and the allowed methods."""
        self._record(self._read_body())
        self._send_empty(
            HTTPStatus.OK,
            extra_headers={
                "DAV": "1, 2, 3, calendar-access",
                "Allow": "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, REPORT",
            },
        )

    def do_PROPFIND(self) -> None:
        """Handle principal/home-set/collection/resource property discovery."""
        body = self._read_body()
        self._record(body)
        _handle_propfind(self.fake, self, body)

    def do_REPORT(self) -> None:
        """Handle a calendar-query REPORT, with optional time-range filtering."""
        body = self._read_body()
        self._record(body)
        _handle_report(self.fake, self, body)

    def do_PUT(self) -> None:
        """Create or update a resource, honouring If-Match and force-412 mode."""
        body = self._read_body()
        self._record(body)
        _handle_put(self.fake, self, body)

    def do_DELETE(self) -> None:
        """Delete a resource, honouring If-Match."""
        body = self._read_body()
        self._record(body)
        _handle_delete(self.fake, self)


def _handle_put(fake: FakeCalDAVServer, handler: _Handler, body: bytes) -> None:
    path = urlsplit(handler.path).path
    if_match = handler.headers.get("If-Match")

    if fake.force_412_on_put:
        handler._send_empty(HTTPStatus.PRECONDITION_FAILED)
        return

    found = fake.find_resource(path)
    if found is None:
        collection = fake.find_collection_for_resource_path(path)
        if collection is None:
            handler._send_empty(HTTPStatus.CONFLICT)
            return
        if if_match is not None:
            handler._send_empty(HTTPStatus.PRECONDITION_FAILED)
            return
        name = path.rsplit("/", 1)[-1]
        resource = FakeResource(name=name, ics=body)
        collection.add(resource)
        handler._send_empty(HTTPStatus.CREATED, extra_headers={"ETag": resource.etag})
        return

    _collection, resource = found
    if if_match is not None and if_match not in (resource.etag, "*"):
        handler._send_empty(HTTPStatus.PRECONDITION_FAILED)
        return
    resource.replace(body)
    handler._send_empty(HTTPStatus.NO_CONTENT, extra_headers={"ETag": resource.etag})


def _handle_delete(fake: FakeCalDAVServer, handler: _Handler) -> None:
    path = urlsplit(handler.path).path
    found = fake.find_resource(path)
    if found is None:
        handler._send_empty(HTTPStatus.NOT_FOUND)
        return
    collection, resource = found
    if_match = handler.headers.get("If-Match")
    if if_match is not None and if_match not in (resource.etag, "*"):
        handler._send_empty(HTTPStatus.PRECONDITION_FAILED)
        return
    del collection.resources[resource.name]
    handler._send_empty(HTTPStatus.NO_CONTENT)


def _handle_propfind(fake: FakeCalDAVServer, handler: _Handler, body: bytes) -> None:
    path = urlsplit(handler.path).path
    depth = handler.headers.get("Depth", "0")
    requested = _parse_requested_props(body)

    if path in (fake.principal_path, "/"):
        all_props = {
            "current-user-principal": _prop_current_user_principal(fake),
            "calendar-home-set": _prop_calendar_home_set(fake),
            "displayname": _prop_displayname("principal"),
            "resourcetype": _prop_resourcetype_collection(),
        }
        responses = [_response(fake.principal_path, _select_props(all_props, requested))]
        handler._send_multistatus(responses)
        return

    if path == fake.home_path:
        all_props = {
            "resourcetype": _prop_resourcetype_collection(),
            "displayname": _prop_displayname("home"),
        }
        responses = [_response(fake.home_path, _select_props(all_props, requested))]
        if depth != "0":
            responses.extend(
                _collection_response(fake, collection, requested)
                for collection in fake.ordered_collections()
            )
        handler._send_multistatus(responses)
        return

    collection = fake.find_collection(path)
    if collection is not None:
        responses = [_collection_response(fake, collection, requested)]
        if depth != "0":
            responses.extend(
                _resource_response(fake, collection, resource, requested)
                for resource in fake.ordered_resources(collection)
            )
        handler._send_multistatus(responses)
        return

    found = fake.find_resource(path)
    if found is not None:
        collection, resource = found
        handler._send_multistatus([_resource_response(fake, collection, resource, requested)])
        return

    handler._send_empty(HTTPStatus.NOT_FOUND)


def _handle_report(fake: FakeCalDAVServer, handler: _Handler, body: bytes) -> None:
    path = urlsplit(handler.path).path
    collection = fake.find_collection(path)
    if collection is None:
        handler._send_empty(HTTPStatus.NOT_FOUND)
        return
    if not fake.advertise_calendar_query:
        handler._send_empty(HTTPStatus.FORBIDDEN)
        return

    requested: set[str] | None = None
    time_range: _TimeRange | None = None
    if body.strip():
        root = ET.fromstring(body)
        prop_el = root.find(f"{{{DAV_NS}}}prop")
        if prop_el is not None:
            requested = {_local_name(child.tag) for child in prop_el}
        time_range = _extract_time_range(root)

    responses = [
        _resource_response(fake, collection, resource, requested)
        for resource in fake.ordered_resources(collection)
        if time_range is None or _resource_in_time_range(resource, time_range)
    ]
    handler._send_multistatus(responses)


def _parse_requested_props(body: bytes) -> set[str] | None:
    if not body.strip():
        return None
    root = ET.fromstring(body)
    prop_el = root.find(f"{{{DAV_NS}}}prop")
    if prop_el is None:
        return None
    return {_local_name(child.tag) for child in prop_el}


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _select_props(all_props: dict[str, str], requested: set[str] | None) -> list[str]:
    if requested is None:
        return list(all_props.values())
    return [xml for name, xml in all_props.items() if name in requested]


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _multistatus(responses: list[str]) -> bytes:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<D:multistatus xmlns:D="{DAV_NS}" xmlns:C="{CALDAV_NS}">'
        + "".join(responses)
        + "</D:multistatus>"
    )
    return body.encode("utf-8")


def _response(href: str, props_xml: list[str]) -> str:
    props = "".join(props_xml)
    return (
        f"<D:response><D:href>{_xml_escape(href)}</D:href>"
        f"<D:propstat><D:prop>{props}</D:prop>"
        "<D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
        "</D:response>"
    )


def _collection_response(
    fake: FakeCalDAVServer, collection: FakeCollection, requested: set[str] | None
) -> str:
    all_props = {
        "resourcetype": _prop_resourcetype_calendar(),
        "displayname": _prop_displayname(collection.display_name),
        "supported-calendar-component-set": _prop_supported_calendar_component_set(
            collection.supported_components
        ),
        "supported-report-set": _prop_supported_report_set(fake.advertise_calendar_query),
    }
    return _response(fake.collection_href(collection), _select_props(all_props, requested))


def _resource_response(
    fake: FakeCalDAVServer,
    collection: FakeCollection,
    resource: FakeResource,
    requested: set[str] | None,
) -> str:
    all_props = {
        "getetag": _prop_getetag(resource),
        "getcontenttype": _prop_getcontenttype(),
        "calendar-data": _prop_calendar_data(resource),
    }
    return _response(fake.resource_href(collection, resource), _select_props(all_props, requested))


def _prop_current_user_principal(fake: FakeCalDAVServer) -> str:
    href = fake.principal_path
    return f"<D:current-user-principal><D:href>{href}</D:href></D:current-user-principal>"


def _prop_calendar_home_set(fake: FakeCalDAVServer) -> str:
    return f"<C:calendar-home-set><D:href>{fake.home_path}</D:href></C:calendar-home-set>"


def _prop_resourcetype_collection() -> str:
    return "<D:resourcetype><D:collection/></D:resourcetype>"


def _prop_resourcetype_calendar() -> str:
    return "<D:resourcetype><D:collection/><C:calendar/></D:resourcetype>"


def _prop_displayname(name: str) -> str:
    return f"<D:displayname>{_xml_escape(name)}</D:displayname>"


def _prop_supported_calendar_component_set(components: tuple[str, ...]) -> str:
    comps = "".join(f'<C:comp name="{c}"/>' for c in components)
    return f"<C:supported-calendar-component-set>{comps}</C:supported-calendar-component-set>"


def _prop_supported_report_set(advertise_calendar_query: bool) -> str:
    reports = ["<D:supported-report><D:report><D:sync-collection/></D:report></D:supported-report>"]
    if advertise_calendar_query:
        reports.append(
            "<D:supported-report><D:report><C:calendar-query/></D:report></D:supported-report>"
        )
    reports.append(
        "<D:supported-report><D:report><C:calendar-multiget/></D:report></D:supported-report>"
    )
    return f"<D:supported-report-set>{''.join(reports)}</D:supported-report-set>"


def _prop_getetag(resource: FakeResource) -> str:
    return f"<D:getetag>{resource.etag}</D:getetag>"


def _prop_getcontenttype() -> str:
    return "<D:getcontenttype>text/calendar; charset=utf-8</D:getcontenttype>"


def _prop_calendar_data(resource: FakeResource) -> str:
    return f"<C:calendar-data>{_xml_escape(resource.ics.decode('utf-8'))}</C:calendar-data>"


def _extract_time_range(root: ET.Element) -> _TimeRange | None:
    time_range_el = root.find(f".//{{{CALDAV_NS}}}time-range")
    if time_range_el is None:
        return None
    start_s = time_range_el.get("start")
    end_s = time_range_el.get("end")
    start = _parse_ical_utc(start_s) if start_s else None
    end = _parse_ical_utc(end_s) if end_s else None
    return _TimeRange(start=start, end=end)


def _parse_ical_utc(value: str) -> datetime.datetime:
    return datetime.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.UTC)


def _resource_in_time_range(resource: FakeResource, time_range: _TimeRange) -> bool:
    calendar = icalendar.Calendar.from_ical(resource.ics)
    for component in calendar.walk("VEVENT"):
        start = _component_start(component)
        if start is None:
            continue
        end = _component_end(component, start)
        if time_range.end is not None and start >= time_range.end:
            continue
        if time_range.start is not None and end is not None and end <= time_range.start:
            continue
        return True
    return False


def _component_start(component: Any) -> datetime.datetime | None:  # noqa: ANN401
    # `component` comes straight out of the untyped `icalendar` package (no stubs);
    # narrowed with `isinstance` immediately below rather than typed against it.
    dtstart = component.get("DTSTART")
    if dtstart is None:
        return None
    value = dtstart.dt
    assert isinstance(value, datetime.date)
    return _to_utc_datetime(value)


def _component_end(
    component: Any,  # noqa: ANN401
    start: datetime.datetime | None,
) -> datetime.datetime | None:
    # Same untyped-`icalendar` rationale as `_component_start` above.
    dtend = component.get("DTEND")
    if dtend is not None:
        value = dtend.dt
        assert isinstance(value, datetime.date)
        return _to_utc_datetime(value)
    duration = component.get("DURATION")
    if duration is not None and start is not None:
        duration_value = duration.dt
        assert isinstance(duration_value, datetime.timedelta)
        return start + duration_value
    return start


def _to_utc_datetime(value: datetime.date | datetime.datetime) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=datetime.UTC)
        return value.astimezone(datetime.UTC)
    return datetime.datetime(value.year, value.month, value.day, tzinfo=datetime.UTC)
