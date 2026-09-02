"""Thin, fully-typed wrapper over ``caldav``: If-Match, Schedule-Reply, and ETags.

This is the only module allowed to touch the ``caldav`` package directly
(CLAUDE.md §"non-negotiable invariants"). It exposes fully typed domain
objects — :class:`CalendarCollection`, :class:`CalendarResource`,
:class:`Capabilities` — to the rest of the code, so ``Any`` from the
untyped third-party surface never leaks further. Every mutation call
carries ``If-Match`` and ``Schedule-Reply: F`` (RFC 6638; brief §7).

Authentication is built in exactly one place, :func:`_connect`: today it
always constructs HTTP Basic auth from :class:`~arwen.config.Credentials`.
A second scheme (bearer, digest) would only change that one function's
body — nothing in :class:`DavConnection` or in :mod:`arwen.discovery`
depends on how the auth object was built.

RFC 4791 ``time-range`` queries go server-side via ``REPORT``
``calendar-query`` when :class:`Capabilities` reports it is supported
(brief §4); otherwise :meth:`DavConnection.list_resources` falls back to a
plain depth-1 ``PROPFIND`` and filters client-side. The two paths must
agree — asserted directly in the integration tests. The client-side
filter's only call into ``recurring-ical-events`` goes through
:func:`arwen.recurrence.expand_occurrences`, per CLAUDE.md §1.1: this
module never calls that library directly.

Response bodies are parsed with the standard library's
``xml.etree.ElementTree`` rather than ``caldav``'s own (``lxml``-backed,
``Any``-typed) multistatus parser, which keeps this module's public surface
fully annotated without depending on ``lxml`` stubs.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from caldav.davclient import DAVClient
from caldav.lib import error as caldav_error
from icalendar import Calendar

from arwen.model import EventTime, FloatingDateTime, Instant
from arwen.recurrence import expand_occurrences, start_of

if TYPE_CHECKING:
    from collections.abc import Mapping

    from caldav.response import DAVResponse

    from arwen.config import Credentials

_DAV_NS = "DAV:"
_CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
_ICAL_UTC_FORMAT = "%Y%m%dT%H%M%SZ"

_SCHEDULE_REPLY_HEADERS: dict[str, str] = {"Schedule-Reply": "F"}
"""Carried on every mutating request. Never emit iTIP scheduling messages (RFC 6638)."""

_MULTISTATUS_OK: frozenset[int] = frozenset({200, 207})
_PUT_OK: frozenset[int] = frozenset({200, 201, 204})
_DELETE_OK: frozenset[int] = frozenset({200, 204})
_PRECONDITION_FAILED = 412


class DavError(Exception):
    """Base class for DAV-layer failures."""


class DavConnectionError(DavError):
    """Raised when the server cannot be reached, or authentication fails."""


class PreconditionFailedError(DavError):
    """Raised when a mutation's ``If-Match`` precondition failed (412), brief §7."""

    def __init__(self, href: str) -> None:
        """Record which resource's precondition failed."""
        self.href = href
        super().__init__(f"precondition failed for {href}")


class DavRequestError(DavError):
    """Raised when a DAV request fails with an unexpected HTTP status."""

    def __init__(self, method: str, href: str, status: int, reason: str) -> None:
        """Record the failed request's method, target, and the server's response."""
        self.method = method
        self.href = href
        self.status = status
        super().__init__(f"{method} {href} failed: {status} {reason}")


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Server capabilities discovered for one collection, per brief §4.

    ``calendar_access`` comes from the ``DAV:`` header of an ``OPTIONS``
    request; ``supports_calendar_query`` from that collection's
    ``supported-report-set``, and decides whether
    :meth:`DavConnection.list_resources` can filter by ``time-range``
    server-side or must fall back to client-side filtering.
    """

    calendar_access: bool
    supports_calendar_query: bool


@dataclass(frozen=True, slots=True)
class CalendarCollection:
    """One calendar collection under the calendar-home-set, per brief §4."""

    href: str
    display_name: str
    supported_components: frozenset[str]


@dataclass(frozen=True, slots=True)
class CalendarResource:
    """One calendar resource (a single ``.ics``), with the ETag it was read at.

    :attr:`etag` is what a later mutation must send back as ``If-Match``
    (brief §7); it is never re-derived from :attr:`calendar`.
    """

    href: str
    etag: str
    calendar: Calendar


@dataclass(frozen=True, slots=True)
class TimeRange:
    """An RFC 4791 ``time-range`` query bound.

    ``end`` is exclusive and mandatory; ``start`` is optional, since RFC
    4791 §9.9 allows a ``time-range`` to carry only ``end``. arwen's one
    range query in this iteration (``delete before``) has no lower bound —
    a ``--from`` variant is out of scope (brief §12) — so ``start`` stays
    ``None`` in practice, but is kept here for protocol completeness.
    """

    end: datetime
    start: datetime | None = None

    def __post_init__(self) -> None:
        """Reject naive bounds; an RFC 4791 time-range is always in UTC."""
        if self.end.tzinfo is None:
            raise ValueError("TimeRange.end must be timezone-aware")
        if self.start is not None and self.start.tzinfo is None:
            raise ValueError("TimeRange.start must be timezone-aware")


def _connect(credentials: Credentials, *, timeout: int | None) -> DAVClient:
    """Build the authenticated ``DAVClient`` — the single point auth is constructed.

    Only HTTP Basic auth (brief §3) is implemented today. Adding a second
    scheme (a bearer token, say) only changes this function's body; no
    caller of :class:`DavConnection` needs to change.
    """
    return DAVClient(
        url=credentials.url,
        username=credentials.username,
        password=credentials.password,
        auth_type="basic",
        timeout=timeout,
    )


@dataclass(frozen=True, slots=True)
class _XmlResponse:
    """One ``<D:response>`` entry of a parsed multistatus body."""

    href: str
    props: dict[str, ET.Element]


def _local_name(tag: str) -> str:
    """Strip an XML namespace URI from a Clark-notation tag, e.g. ``{DAV:}href`` -> ``href``."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _body(response: DAVResponse) -> str:
    """Return a response's body as ``str``, typed explicitly at this untyped boundary."""
    raw: str = response.raw
    return raw


def _header(response: DAVResponse, name: str) -> str | None:
    """Return one response header, typed explicitly at this untyped boundary."""
    headers: Mapping[str, str] = response.headers
    return headers.get(name)


def _require_multistatus(method: str, href: str, response: DAVResponse) -> None:
    """Raise :class:`DavRequestError` unless ``response`` is a successful multistatus."""
    status: int = response.status
    if status not in _MULTISTATUS_OK:
        raise DavRequestError(method, href, status, response.reason)


def _parse_multistatus(xml_text: str) -> list[_XmlResponse]:
    """Parse a WebDAV multistatus body into one :class:`_XmlResponse` per ``<D:response>``.

    Every ``<D:prop>`` across every ``<D:propstat>`` of a response is
    collected into one flat, local-name-keyed dict; a property this run
    never asked for or that the server could not supply simply never
    appears, so callers read it with ``dict.get``.
    """
    if not xml_text.strip():
        return []
    root = ET.fromstring(xml_text)
    responses: list[_XmlResponse] = []
    for response_el in root.findall(f"{{{_DAV_NS}}}response"):
        href_el = response_el.find(f"{{{_DAV_NS}}}href")
        href = href_el.text if href_el is not None and href_el.text else ""
        props: dict[str, ET.Element] = {}
        for propstat_el in response_el.findall(f"{{{_DAV_NS}}}propstat"):
            prop_el = propstat_el.find(f"{{{_DAV_NS}}}prop")
            if prop_el is None:
                continue
            for child in prop_el:
                props[_local_name(child.tag)] = child
        responses.append(_XmlResponse(href=href, props=props))
    return responses


def _text_of(element: ET.Element | None) -> str:
    """Return an element's text content, or ``""`` if the element is absent or empty."""
    return (element.text or "") if element is not None else ""


def _href_child_text(element: ET.Element | None) -> str | None:
    """Return the ``<D:href>`` text nested inside ``element``, or ``None``."""
    if element is None:
        return None
    href = element.find(f"{{{_DAV_NS}}}href")
    return href.text if href is not None and href.text else None


def _is_calendar_collection(element: ET.Element | None) -> bool:
    """Report whether a ``resourcetype`` element carries ``<C:calendar/>``."""
    return element is not None and element.find(f"{{{_CALDAV_NS}}}calendar") is not None


def _component_set(element: ET.Element | None) -> frozenset[str]:
    """Extract the component names of a ``supported-calendar-component-set`` element."""
    if element is None:
        return frozenset()
    names = (child.get("name") for child in element.findall(f"{{{_CALDAV_NS}}}comp"))
    return frozenset(name for name in names if name)


def _supports_calendar_query(element: ET.Element | None) -> bool:
    """Report whether a ``supported-report-set`` element advertises ``calendar-query``."""
    return element is not None and element.find(f".//{{{_CALDAV_NS}}}calendar-query") is not None


def _supports_calendar_access(response: DAVResponse) -> bool:
    """Report whether an ``OPTIONS`` response's ``DAV:`` header lists ``calendar-access``."""
    dav_header = _header(response, "DAV") or ""
    tokens = {token.strip().lower() for token in dav_header.split(",")}
    return "calendar-access" in tokens


def _resource_from_response(item: _XmlResponse) -> CalendarResource | None:
    """Build a :class:`CalendarResource` from a multistatus entry, or ``None`` if it isn't one.

    An entry with no ``calendar-data`` is the collection's own self-entry
    (present in a depth-1 response alongside its children), not a resource.

    Raises:
        DavError: If ``calendar-data`` is present but does not parse as a
            single ``VCALENDAR``.
    """
    calendar_data_element = item.props.get("calendar-data")
    if calendar_data_element is None:
        return None
    parsed = Calendar.from_ical(_text_of(calendar_data_element))
    if not isinstance(parsed, Calendar):
        raise DavError(f"resource at {item.href} did not parse as a single VCALENDAR")
    return CalendarResource(
        href=item.href, etag=_text_of(item.props.get("getetag")), calendar=parsed
    )


def _propfind_body(props: list[str]) -> str:
    """Build a ``PROPFIND`` request body asking for exactly ``props``.

    Each entry in ``props`` is a namespace-prefixed tag, e.g.
    ``"D:displayname"`` or ``"C:calendar-data"``.
    """
    inner = "".join(f"<{prop}/>" for prop in props)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<D:propfind xmlns:D="{_DAV_NS}" xmlns:C="{_CALDAV_NS}">'
        f"<D:prop>{inner}</D:prop></D:propfind>"
    )


def _calendar_query_body(time_range: TimeRange) -> str:
    """Build a ``calendar-query`` REPORT body filtering ``VEVENT``s by ``time_range``."""
    end_attr = f' end="{time_range.end.astimezone(UTC).strftime(_ICAL_UTC_FORMAT)}"'
    start_attr = (
        f' start="{time_range.start.astimezone(UTC).strftime(_ICAL_UTC_FORMAT)}"'
        if time_range.start is not None
        else ""
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<C:calendar-query xmlns:D="{_DAV_NS}" xmlns:C="{_CALDAV_NS}">'
        "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
        '<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">'
        f"<C:time-range{start_attr}{end_attr}/>"
        "</C:comp-filter></C:comp-filter></C:filter>"
        "</C:calendar-query>"
    )


def _as_utc(value: EventTime) -> datetime:
    """Coerce an :data:`~arwen.model.EventTime` to a UTC instant for window arithmetic.

    Used only to build a safe, tight lower bound for the client-side
    ``time-range`` fallback (:func:`_lower_bound`) — never for a pruning or
    business-logic comparison, which is brief §5.1's resolved-zone job in
    :mod:`arwen.recurrence`, not this module's.
    """
    if isinstance(value, Instant):
        return value.value
    if isinstance(value, FloatingDateTime):
        return value.value.replace(tzinfo=UTC)
    return datetime.combine(value.value, time.min, tzinfo=UTC)


def _lower_bound(calendar: Calendar, end: datetime) -> datetime:
    """Return a safe lower bound for expanding ``calendar`` up to ``end``.

    One day before the earliest ``DTSTART`` across the resource's event
    components, so :func:`_matches_time_range` never has to expand from an
    arbitrarily distant epoch. Falls back to just before ``end`` if the
    resource has no readable ``DTSTART`` at all.
    """
    starts: list[datetime] = []
    for component in calendar.walk("VEVENT"):
        try:
            starts.append(_as_utc(start_of(component)))
        except ValueError:
            continue
    if not starts:
        return end - timedelta(days=1)
    return min(starts) - timedelta(days=1)


def _matches_time_range(calendar: Calendar, time_range: TimeRange) -> bool:
    """Report whether ``calendar`` has any occurrence overlapping ``time_range``.

    The client-side counterpart of a server-side ``REPORT``
    ``calendar-query`` ``time-range`` filter — used when
    :attr:`Capabilities.supports_calendar_query` is ``False``. Expansion is
    the one call into ``recurring-ical-events`` this module makes, and it
    goes through :func:`arwen.recurrence.expand_occurrences` (CLAUDE.md
    §1.1), never the library directly.
    """
    lower = (
        time_range.start if time_range.start is not None else _lower_bound(calendar, time_range.end)
    )
    return bool(expand_occurrences(calendar, lower, time_range.end))


class DavConnection:
    """A connection to one CalDAV server, exposing typed discovery and mutation operations."""

    def __init__(self, credentials: Credentials, *, timeout: int | None = 30) -> None:
        """Open a connection authenticated from ``credentials`` (brief §3)."""
        self._client = _connect(credentials, timeout=timeout)

    def _absolute(self, href: str) -> str:
        """Resolve a possibly relative ``href`` against this connection's server URL."""
        return str(self._client.url.join(href))

    def _request(
        self,
        method: str,
        href: str,
        body: str = "",
        headers: Mapping[str, str] | None = None,
    ) -> DAVResponse:
        """Issue one HTTP request, wrapping connection/authentication failures.

        Raises:
            DavConnectionError: If the server cannot be reached, or
                authentication is rejected.
        """
        try:
            return self._client.request(self._absolute(href), method, body, headers or {})
        except caldav_error.AuthorizationError as exc:
            raise DavConnectionError(str(exc)) from exc
        except OSError as exc:
            raise DavConnectionError(str(exc)) from exc

    def discover_principal(self) -> str:
        """Discover the current-user-principal href, per brief §4 step 1.

        Raises:
            DavError: If the server does not report a
                ``current-user-principal``.
        """
        response = self._request(
            "PROPFIND", "", _propfind_body(["D:current-user-principal"]), {"Depth": "0"}
        )
        _require_multistatus("PROPFIND", "", response)
        responses = _parse_multistatus(_body(response))
        prop = responses[0].props.get("current-user-principal") if responses else None
        principal = _href_child_text(prop)
        if principal is None:
            raise DavError("server did not report a current-user-principal")
        return principal

    def discover_calendar_home_set(self, principal_href: str) -> str:
        """Discover a principal's calendar-home-set href, per brief §4 step 1.

        Raises:
            DavError: If the principal does not report a
                ``calendar-home-set``.
        """
        response = self._request(
            "PROPFIND",
            principal_href,
            _propfind_body(["C:calendar-home-set"]),
            {"Depth": "0"},
        )
        _require_multistatus("PROPFIND", principal_href, response)
        responses = _parse_multistatus(_body(response))
        prop = responses[0].props.get("calendar-home-set") if responses else None
        home_href = _href_child_text(prop)
        if home_href is None:
            raise DavError("principal did not report a calendar-home-set")
        return home_href

    def list_calendars(self, home_href: str) -> list[CalendarCollection]:
        """List every calendar collection under ``home_href``, per brief §4 step 2.

        Non-calendar members of the home set (and the home collection's own
        self-entry, always present in a depth-1 response) are excluded by
        checking ``resourcetype`` for ``<C:calendar/>``.
        """
        response = self._request(
            "PROPFIND",
            home_href,
            _propfind_body(
                ["D:resourcetype", "D:displayname", "C:supported-calendar-component-set"]
            ),
            {"Depth": "1"},
        )
        _require_multistatus("PROPFIND", home_href, response)
        collections: list[CalendarCollection] = []
        for item in _parse_multistatus(_body(response)):
            if not _is_calendar_collection(item.props.get("resourcetype")):
                continue
            collections.append(
                CalendarCollection(
                    href=item.href,
                    display_name=_text_of(item.props.get("displayname")) or item.href,
                    supported_components=_component_set(
                        item.props.get("supported-calendar-component-set")
                    ),
                )
            )
        return collections

    def discover_capabilities(self, href: str) -> Capabilities:
        """Discover server capabilities for one collection, per brief §4.

        Issues an ``OPTIONS`` request and checks the ``DAV:`` header for
        ``calendar-access``, then a ``PROPFIND`` for ``supported-report-set``
        to decide whether a server-side ``time-range`` filter is available.
        """
        options_response = self._request("OPTIONS", href)
        calendar_access = _supports_calendar_access(options_response)

        propfind_response = self._request(
            "PROPFIND", href, _propfind_body(["D:supported-report-set"]), {"Depth": "0"}
        )
        _require_multistatus("PROPFIND", href, propfind_response)
        responses = _parse_multistatus(_body(propfind_response))
        supports_query = bool(responses) and _supports_calendar_query(
            responses[0].props.get("supported-report-set")
        )
        return Capabilities(calendar_access=calendar_access, supports_calendar_query=supports_query)

    def list_resources(
        self,
        calendar_href: str,
        capabilities: Capabilities,
        time_range: TimeRange | None = None,
    ) -> list[CalendarResource]:
        """List the resources of one calendar, per brief §4/§5.

        Uses a server-side ``REPORT`` ``calendar-query`` with a
        ``time-range`` filter when ``time_range`` is given and the server
        supports it (:attr:`Capabilities.supports_calendar_query`);
        otherwise falls back to a plain depth-1 ``PROPFIND`` and filters
        client-side. Both paths must agree — asserted directly in the
        integration tests (brief §4).
        """
        if time_range is not None and capabilities.supports_calendar_query:
            return self._report_calendar_query(calendar_href, time_range)
        resources = self._propfind_resources(calendar_href)
        if time_range is None:
            return resources
        return [
            resource for resource in resources if _matches_time_range(resource.calendar, time_range)
        ]

    def _propfind_resources(self, calendar_href: str) -> list[CalendarResource]:
        """List every resource of a calendar via a plain depth-1 ``PROPFIND``, unfiltered."""
        response = self._request(
            "PROPFIND",
            calendar_href,
            _propfind_body(["D:getetag", "C:calendar-data"]),
            {"Depth": "1"},
        )
        _require_multistatus("PROPFIND", calendar_href, response)
        return [
            resource
            for item in _parse_multistatus(_body(response))
            if (resource := _resource_from_response(item)) is not None
        ]

    def _report_calendar_query(
        self, calendar_href: str, time_range: TimeRange
    ) -> list[CalendarResource]:
        """List a calendar's resources via a server-side ``calendar-query`` ``REPORT``."""
        response = self._request(
            "REPORT", calendar_href, _calendar_query_body(time_range), {"Depth": "1"}
        )
        _require_multistatus("REPORT", calendar_href, response)
        return [
            resource
            for item in _parse_multistatus(_body(response))
            if (resource := _resource_from_response(item)) is not None
        ]

    def put_resource(self, href: str, calendar: Calendar, *, if_match: str) -> str:
        """Write a mutated resource back, per brief §5.4 step 5 / §7.

        Carries ``If-Match`` with the ETag read during the scan (brief §7)
        and ``Schedule-Reply: F`` (RFC 6638) so no attendee is ever
        notified. Returns the new ETag, re-reading it with a ``PROPFIND``
        if the ``PUT`` response did not include one.

        Raises:
            PreconditionFailedError: On a 412 conflict.
            DavRequestError: On any other non-success status.
        """
        body = calendar.to_ical().decode("utf-8")
        headers = {
            "Content-Type": "text/calendar; charset=utf-8",
            "If-Match": if_match,
            **_SCHEDULE_REPLY_HEADERS,
        }
        response = self._request("PUT", href, body, headers)
        status: int = response.status
        if status == _PRECONDITION_FAILED:
            raise PreconditionFailedError(href)
        if status not in _PUT_OK:
            raise DavRequestError("PUT", href, status, response.reason)
        etag = _header(response, "ETag")
        return etag if etag is not None else self._refetch_etag(href)

    def delete_resource(self, href: str, *, if_match: str) -> None:
        """Delete a resource, per brief §7.

        Carries ``If-Match`` and ``Schedule-Reply: F``, exactly like
        :meth:`put_resource`.

        Raises:
            PreconditionFailedError: On a 412 conflict.
            DavRequestError: On any other non-success status.
        """
        headers = {"If-Match": if_match, **_SCHEDULE_REPLY_HEADERS}
        response = self._request("DELETE", href, "", headers)
        status: int = response.status
        if status == _PRECONDITION_FAILED:
            raise PreconditionFailedError(href)
        if status not in _DELETE_OK:
            raise DavRequestError("DELETE", href, status, response.reason)

    def _refetch_etag(self, href: str) -> str:
        """Read back a resource's current ETag with a ``PROPFIND``.

        Used when a ``PUT`` response did not carry an ``ETag`` header;
        RFC 4791 recommends but does not require the server to send one.
        """
        response = self._request("PROPFIND", href, _propfind_body(["D:getetag"]), {"Depth": "0"})
        _require_multistatus("PROPFIND", href, response)
        responses = _parse_multistatus(_body(response))
        if not responses:
            raise DavError(f"could not read back the ETag of {href} after PUT")
        return _text_of(responses[0].props.get("getetag"))
