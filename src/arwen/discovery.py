"""Principal/calendar-home-set discovery, capability checks, interactive calendar picker.

Implements brief §4 end to end. Nothing here is cached or persisted between
runs: server-assigned collection identifiers are not assumed to be stable,
so :func:`discover_calendars` re-runs the full principal -> home-set ->
listing chain on every call.
"""

import sys
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from arwen.dav import CalendarCollection, DavConnection


class CalendarSelectionError(Exception):
    """Raised when no calendar can be unambiguously selected, per brief §4."""


def discover_calendars(connection: DavConnection) -> list[CalendarCollection]:
    """Discover the calendars available to the authenticated principal, per brief §4.

    Runs current-user-principal discovery, then calendar-home-set
    discovery, then a depth-1 listing of the home set, keeping only
    collections whose ``supported-calendar-component-set`` includes
    ``VEVENT``.
    """
    principal_href = connection.discover_principal()
    home_href = connection.discover_calendar_home_set(principal_href)
    collections = connection.list_calendars(home_href)
    return [collection for collection in collections if "VEVENT" in collection.supported_components]


def _match_by_display_name(
    calendars: list[CalendarCollection], name: str
) -> CalendarCollection | None:
    """Return the single calendar whose display name is exactly ``name``, or ``None``.

    ``None`` covers both "no match" and "ambiguous match" (brief §4: "If
    the name is absent or ambiguous, fall back to the interactive list.").
    """
    matches = [calendar for calendar in calendars if calendar.display_name == name]
    return matches[0] if len(matches) == 1 else None


def _render_picker(calendars: list[CalendarCollection], output_stream: TextIO) -> None:
    """Write the numbered calendar list brief §4 step 3 describes."""
    for index, calendar in enumerate(calendars, start=1):
        output_stream.write(f"{index}. {calendar.display_name} ({calendar.href})\n")
    output_stream.write("Select a calendar: ")
    output_stream.flush()


def _read_selection(
    calendars: list[CalendarCollection], input_stream: TextIO
) -> CalendarCollection:
    """Read and validate a 1-based selection index from ``input_stream``.

    Raises:
        CalendarSelectionError: If input is closed, not an integer, or out
            of range.
    """
    line = input_stream.readline()
    if not line:
        raise CalendarSelectionError("no selection given (input closed)")

    stripped = line.strip()
    try:
        choice = int(stripped)
    except ValueError as exc:
        raise CalendarSelectionError(f"invalid selection: {stripped!r}") from exc

    if not 1 <= choice <= len(calendars):
        raise CalendarSelectionError(f"selection out of range: {choice}")
    return calendars[choice - 1]


def select_calendar(
    connection: DavConnection,
    *,
    preselect: str | None = None,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> CalendarCollection:
    """Select a calendar for this run, per brief §4.

    ``--calendar NAME`` (``preselect``) matches on display name and skips
    the prompt when it unambiguously identifies one calendar. Otherwise the
    numbered interactive picker is used — unless ``input_stream`` is not a
    TTY, in which case this is a usage error rather than a guess (brief
    §4: "If stdin is not a TTY and no unambiguous ``--calendar`` was given,
    exit with a usage error rather than guessing.").

    Raises:
        CalendarSelectionError: If no ``VEVENT``-capable calendar is
            available, if ``input_stream`` is not interactive and no
            unambiguous ``preselect`` was given, or if the interactive
            selection itself is invalid.
    """
    calendars = discover_calendars(connection)
    if not calendars:
        raise CalendarSelectionError("no calendars with VEVENT support are available")

    if preselect is not None:
        matched = _match_by_display_name(calendars, preselect)
        if matched is not None:
            return matched

    if not input_stream.isatty():
        raise CalendarSelectionError(
            "no unambiguous --calendar match and stdin is not a TTY; refusing to guess"
        )

    _render_picker(calendars, output_stream)
    return _read_selection(calendars, input_stream)
