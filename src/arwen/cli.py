"""Command-line entry point: argparse subparsers and process exit codes.

Wires brief §2's two commands end to end::

    arwen delete before DATE [options]
    arwen delete duplicates  [options]

The order of operations is fixed by the safety model of brief §7 and never
varies: read credentials, connect, let the user pick a calendar (§4),
scan, *plan* every action without touching the server, and only then — and
only under ``--execute`` — write the backup, ``fsync`` it, and issue the
mutations. A dry run stops after the plan, which is what makes "a dry run
issues zero mutating HTTP requests" true by construction rather than by
inspection: the code path that mutates is not entered at all.

Every decision about calendar data is delegated to the pure engines in
:mod:`arwen.recurrence` and :mod:`arwen.dedup`; this module only sequences
them, turns their results into :mod:`arwen.report` entries, and maps the
outcome onto brief §7's exit codes.
"""

import argparse
import logging
import os
import re
import sys
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, override
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from arwen.backup import BackupError, write_backup
from arwen.config import ConfigError, Credentials
from arwen.dav import (
    CalendarResource,
    DavConnection,
    DavError,
    PreconditionFailedError,
    TimeRange,
)
from arwen.dedup import duplicate_key, group_duplicates
from arwen.discovery import CalendarSelectionError, select_calendar
from arwen.model import Action, DedupCandidate
from arwen.recurrence import classify_non_recurring, is_recurring, prune_recurring, start_of
from arwen.report import Report, ReportEntry, format_event_time, render

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from datetime import tzinfo

    from icalendar import Calendar, Component

    from arwen.model import DuplicateGroup

_log = logging.getLogger("arwen")

EXIT_OK = 0
"""Brief §7: the run completed with nothing needing attention."""

EXIT_FINDINGS = 1
"""Brief §7: completed with skips, conflicts, or items needing review.

Also used when a backup could not be written: the run stopped before its
first mutating request, which is a finding the user has to act on, not a
usage error and not a connection failure.
"""

EXIT_USAGE = 2
"""Brief §7: usage error — a malformed argument, or an unresolvable calendar."""

EXIT_CONNECTION = 3
"""Brief §7: the server could not be reached, or authentication failed."""

EXIT_INTERRUPTED = 130
"""Brief §7: interrupted (SIGINT), following the shell's 128 + signal convention."""

_DEFAULT_ENV_FILE = Path("arwen.env")
_DEFAULT_BACKUP_DIR = Path("backups")

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMEZONE_FILE = Path("/etc/timezone")
_LOCALTIME_LINK = Path("/etc/localtime")
_ZONEINFO_MARKER = "zoneinfo"


class _UsageError(Exception):
    """Raised for a malformed argument that argparse itself cannot reject.

    Caught by :func:`main`, which reports it on stderr and exits
    :data:`EXIT_USAGE`.
    """


class _RedactingFormatter(logging.Formatter):
    """Renders log records for the stderr handler, with the password replaced by ``***``.

    Brief §3 forbids the password appearing in any log output, "including
    in verbose HTTP logs and in exception messages". Both halves of that
    sentence decide where the redaction has to live.

    It belongs to the *handler*, not to the root logger. ``logging`` consults
    a logger's own filters only for the records logged through that logger;
    a record from a child logger is passed straight to the ancestors'
    handlers and never meets their filters. Every record in this program
    comes from a child — ``arwen.dav``, ``arwen.config``, ``caldav``,
    ``urllib3`` — so a filter on the root logger would redact nothing at all.

    And it belongs to the formatter rather than to a handler filter, because
    formatting is the one point that sees the rendered message, its
    arguments *and* the formatted traceback. Redacting the returned string
    covers "in exception messages" without having to rewrite a record's
    ``exc_info`` in place.
    """

    def __init__(self, fmt: str) -> None:
        """Render records with ``fmt``, redacting nothing until :meth:`redact` is called."""
        super().__init__(fmt)
        self._secret = ""

    def redact(self, secret: str) -> None:
        """Replace ``secret`` with ``***`` in every record rendered from now on.

        An empty secret disables redaction, which is what this formatter does
        between :func:`_configure_logging` and the moment :func:`_run` has
        read the credential file. Nothing in that window has the password:
        the only record :class:`~arwen.config.Credentials` can emit before
        handing it over is the permissions warning, which names the file.
        """
        self._secret = secret

    @override
    def format(self, record: logging.LogRecord) -> str:
        """Render ``record``, then strike out every occurrence of the secret."""
        rendered = super().format(record)
        if not self._secret:
            return rendered
        return rendered.replace(self._secret, "***")


@dataclass(frozen=True, slots=True)
class _Options:
    """The brief §2 global options, after parsing."""

    execute: bool
    calendar: str | None
    env_file: Path
    backup_dir: Path
    json_output: bool
    verbose: bool


@dataclass(frozen=True, slots=True)
class _Mutation:
    """One planned mutating request, bound to the report entry that describes it.

    ``payload`` distinguishes the two mutations brief §7 allows: ``None``
    means ``DELETE`` the resource, and a calendar means ``PUT`` that
    calendar back. ``entry_index`` lets the executor rewrite the plan's
    entry with what the server actually replied (a 412 conflict, say)
    without rebuilding the report.
    """

    entry_index: int
    resource: CalendarResource
    payload: Calendar | None


@dataclass(frozen=True, slots=True)
class _Plan:
    """Everything decided before a single mutating request is issued.

    Producing this in full, ahead of any mutation, is what makes the backup
    of brief §7 possible: the set of resources about to change is known
    before the first of them changes.
    """

    entries: list[ReportEntry]
    mutations: list[_Mutation]


def _build_parser() -> argparse.ArgumentParser:
    """Build the brief §2 command surface.

    The global options live on a shared parent parser attached to each leaf
    subcommand, so they are spelled identically for ``delete before`` and
    ``delete duplicates`` and appear in both ``--help`` screens.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform changes. Without it, the run is a dry run.",
    )
    common.add_argument(
        "--calendar",
        metavar="NAME",
        default=None,
        help="Preselect a calendar by display name, skipping the interactive picker.",
    )
    common.add_argument(
        "--env-file",
        metavar="PATH",
        type=Path,
        default=_DEFAULT_ENV_FILE,
        help=f"Credentials file. Default: ./{_DEFAULT_ENV_FILE}.",
    )
    common.add_argument(
        "--backup-dir",
        metavar="PATH",
        type=Path,
        default=_DEFAULT_BACKUP_DIR,
        help=f"Directory for pre-mutation backups. Default: ./{_DEFAULT_BACKUP_DIR}.",
    )
    common.add_argument(
        "--json", action="store_true", help="Machine-readable report instead of the human one."
    )
    common.add_argument("--verbose", action="store_true", help="Debug logging to stderr.")

    parser = argparse.ArgumentParser(
        prog="arwen", description="Maintain CalDAV calendars. Dry-run by default."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    delete = commands.add_parser("delete", help="Remove calendar resources.")
    targets = delete.add_subparsers(dest="target", required=True)

    before = targets.add_parser(
        "before",
        parents=[common],
        help="Remove everything that ended strictly before DATE.",
    )
    before.add_argument("date", metavar="DATE", help="ISO calendar date, YYYY-MM-DD.")
    before.add_argument(
        "--tz",
        metavar="IANA_NAME",
        default=None,
        help="Timezone the boundary is resolved in. Default: the machine's local zone.",
    )

    targets.add_parser(
        "duplicates", parents=[common], help="Remove content-identical duplicate events."
    )
    return parser


def _options_from(args: argparse.Namespace) -> _Options:
    """Collect the global options out of the parsed namespace."""
    return _Options(
        execute=bool(args.execute),
        calendar=args.calendar,
        env_file=Path(args.env_file),
        backup_dir=Path(args.backup_dir),
        json_output=bool(args.json),
        verbose=bool(args.verbose),
    )


def _configure_logging(*, verbose: bool) -> _RedactingFormatter:
    """Send logging to stderr, at debug level under ``--verbose`` (brief §8).

    ``force=True`` is deliberate. Plain :func:`logging.basicConfig` does
    nothing at all when the root logger already has a handler, which would
    leave both arwen's output and its brief §3 redaction at the mercy of
    whoever configured logging first.

    Returns:
        The formatter carrying the redaction, so that :func:`_run` can give
        it the password as soon as it has read one.
    """
    formatter = _RedactingFormatter("%(levelname)s %(name)s: %(message)s")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    logging.basicConfig(
        handlers=[handler],
        force=True,
        level=logging.DEBUG if verbose else logging.WARNING,
    )
    return formatter


def parse_boundary(text: str) -> date:
    """Parse the ``DATE`` argument of ``delete before``, per brief §2.

    Only ``YYYY-MM-DD`` is accepted: no time component, and none of the
    other forms :meth:`datetime.date.fromisoformat` tolerates (``YYYYMMDD``,
    ISO week dates), since a date that does not look like the documented
    one is far more likely to be a mistake than an intention.

    Raises:
        _UsageError: If ``text`` is not an ISO calendar date.
    """
    if not _ISO_DATE.match(text):
        raise _UsageError(f"DATE must be an ISO calendar date (YYYY-MM-DD), got {text!r}")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise _UsageError(f"DATE is not a valid calendar date: {text!r}") from exc


def _local_zone_keys() -> Iterator[str]:
    """Yield candidate IANA names for the machine's local zone, best guess first.

    The standard library has no local-zone lookup and brief §1 forbids
    adding one as a dependency, so the three conventional POSIX sources are
    consulted in order: the ``TZ`` environment variable, ``/etc/timezone``,
    and the target of the ``/etc/localtime`` symlink. Each is only a
    candidate — :func:`resolve_timezone` accepts the first that
    :class:`~zoneinfo.ZoneInfo` recognises.
    """
    tz_variable = os.environ.get("TZ", "").strip().lstrip(":")
    if tz_variable:
        yield tz_variable

    with suppress(OSError):
        yield _TIMEZONE_FILE.read_text(encoding="utf-8").strip()

    try:
        target = _LOCALTIME_LINK.resolve(strict=True)
    except OSError:
        return
    parts = target.parts
    if _ZONEINFO_MARKER in parts:
        yield "/".join(parts[parts.index(_ZONEINFO_MARKER) + 1 :])


def resolve_timezone(name: str | None) -> tuple[tzinfo, str]:
    """Resolve the zone the boundary is interpreted in, per brief §5.1.

    ``--tz`` (``name``) wins when given. Otherwise the machine's local zone
    is resolved to a real IANA zone via :func:`_local_zone_keys`, so that a
    boundary in another season still gets that season's UTC offset. Only if
    no IANA name can be determined does this fall back to the current fixed
    offset, with a warning — a fallback that is correct today and may be an
    hour out across a DST transition, which is precisely why it is the last
    resort and is named as such in the report.

    Returns:
        The resolved zone, and the name to print in the report. Brief §5.1
        requires that name always be printed: it is what "before ``DATE``"
        means.

    Raises:
        _UsageError: If ``name`` is given but is not a known IANA zone.
    """
    if name is not None:
        try:
            return ZoneInfo(name), name
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise _UsageError(f"unknown timezone: {name!r}") from exc

    for key in _local_zone_keys():
        try:
            return ZoneInfo(key), key
        except ZoneInfoNotFoundError, ValueError:
            continue

    offset = datetime.now(UTC).astimezone().tzinfo
    if offset is None:
        _log.warning("could not determine the local timezone; falling back to UTC")
        return UTC, "UTC (fallback)"
    _log.warning("could not determine an IANA name for the local timezone; using its offset")
    return offset, f"{offset} (fixed-offset fallback)"


def _primary_event(calendar: Calendar) -> Component | None:
    """Return the component a report line should describe.

    The master component (the first ``VEVENT`` without a
    ``RECURRENCE-ID``), or — for a resource made only of overrides — its
    first override, or ``None`` for a resource with no ``VEVENT`` at all.
    """
    events: list[Component] = calendar.walk("VEVENT")
    for event in events:
        if event.get("RECURRENCE-ID") is None:
            return event
    return events[0] if events else None


def _entry(resource: CalendarResource, action: Action, detail: str | None = None) -> ReportEntry:
    """Build the brief §8 report line for one resource: UID, summary, start, action."""
    event = _primary_event(resource.calendar)
    if event is None:
        return ReportEntry(
            href=resource.href, uid="", summary="", start="", action=action, detail=detail
        )
    try:
        start = format_event_time(start_of(event))
    except ValueError:
        start = ""
    return ReportEntry(
        href=resource.href,
        uid=str(event.get("UID", "")),
        summary=str(event.get("SUMMARY", "")),
        start=start,
        action=action,
        detail=detail,
    )


def _classify_plain(
    events: list[Component], boundary: date, zone: tzinfo
) -> tuple[Action, str | None]:
    """Classify a non-recurring resource against ``DATE``, per brief §5.3.

    A well-formed non-recurring resource holds exactly one ``VEVENT``, but
    nothing forbids several. The resource is the unit of deletion, so it is
    only deleted when *every* component in it has ended; one straddling
    component makes the whole resource straddle, and any surviving future
    component leaves it untouched.
    """
    actions: list[Action] = []
    for event in events:
        try:
            actions.append(classify_non_recurring(event, boundary, zone))
        except (ValueError, TypeError) as exc:
            return Action.SKIP_NOT_EXAMINED, f"cannot read the event's times: {exc}"

    if Action.SKIP_STRADDLING in actions:
        return Action.SKIP_STRADDLING, "starts before DATE but is still in progress; not truncated"
    if all(action is Action.DELETE for action in actions):
        return Action.DELETE, None
    return Action.UNTOUCHED, None


def _plan_delete_before(
    resources: Sequence[CalendarResource], boundary: date, zone: tzinfo, now: datetime
) -> _Plan:
    """Plan ``delete before DATE`` over the scanned resources, per brief §5.

    Recurring resources (any component with ``RRULE``, ``RDATE``, or
    ``RECURRENCE-ID``) go to the §5.4 pruning engine; everything else is
    classified by §5.3. Nothing is issued here — the plan is complete
    before the first request, which is what the backup of §7 depends on.
    """
    plan = _Plan(entries=[], mutations=[])
    for resource in resources:
        events: list[Component] = resource.calendar.walk("VEVENT")
        if not events:
            plan.entries.append(
                _entry(resource, Action.SKIP_NOT_EXAMINED, "resource has no VEVENT component")
            )
            continue

        if is_recurring(resource.calendar):
            _plan_recurring(plan, resource, boundary, zone, now)
            continue

        action, detail = _classify_plain(events, boundary, zone)
        if action is Action.UNTOUCHED:
            continue
        plan.entries.append(_entry(resource, action, detail))
        if action is Action.DELETE:
            plan.mutations.append(
                _Mutation(entry_index=len(plan.entries) - 1, resource=resource, payload=None)
            )
    return plan


def _plan_recurring(
    plan: _Plan, resource: CalendarResource, boundary: date, zone: tzinfo, now: datetime
) -> None:
    """Run the §5.4 pruning engine over one recurring resource and record its outcome."""
    result = prune_recurring(resource.calendar, boundary, zone, now=now)

    if result.action is Action.UNTOUCHED:
        return
    if result.action is Action.SKIP_UNPRUNABLE:
        plan.entries.append(
            _entry(
                resource,
                Action.SKIP_UNPRUNABLE,
                "no pruning strategy reproduced the surviving occurrences exactly",
            )
        )
        return

    if result.action is Action.DELETE:
        detail = f"every one of its {result.removed} occurrence(s) ends before DATE"
    else:
        strategy = result.strategy.value if result.strategy is not None else "unknown"
        detail = f"drops {result.removed} occurrence(s), keeps {result.kept}, via {strategy}"

    plan.entries.append(_entry(resource, result.action, detail))
    plan.mutations.append(
        _Mutation(
            entry_index=len(plan.entries) - 1,
            resource=resource,
            payload=result.calendar if result.action is Action.MODIFY else None,
        )
    )


def _dedup_triage(
    resources: Sequence[CalendarResource],
) -> tuple[list[ReportEntry], list[DedupCandidate]]:
    """Split a collection into brief §6.1's "not examined" entries and the rest.

    Recurring resources, anything that is not a readable ``VEVENT``, and
    anything whose duplicate key cannot be derived are reported rather than
    silently dropped: a resource missing from the report would be
    indistinguishable from one the run never saw.

    Returns:
        The report entries for everything excluded, and the candidates for
        :func:`~arwen.dedup.group_duplicates`, in the order given.
    """
    skipped: list[ReportEntry] = []
    candidates: list[DedupCandidate] = []
    for resource in resources:
        if not resource.calendar.walk("VEVENT"):
            skipped.append(
                _entry(resource, Action.SKIP_NOT_EXAMINED, "not a VEVENT resource (brief §6.1)")
            )
            continue
        if is_recurring(resource.calendar):
            skipped.append(
                _entry(
                    resource,
                    Action.SKIP_RECURRING,
                    "recurring resources are not de-duplicated in this version",
                )
            )
            continue
        try:
            duplicate_key(resource.calendar)
        except (ValueError, TypeError) as exc:
            skipped.append(
                _entry(resource, Action.SKIP_NOT_EXAMINED, f"cannot derive a duplicate key: {exc}")
            )
            continue
        candidates.append(
            DedupCandidate(href=resource.href, etag=resource.etag, calendar=resource.calendar)
        )
    return skipped, candidates


def _record_duplicate_group(
    plan: _Plan, group: DuplicateGroup, by_href: Mapping[str, CalendarResource]
) -> None:
    """Append one duplicate key group's entries and deletions to ``plan``, per brief §6.3.

    A *mixed* key group — more than one distinct content sub-group under one
    key — is where the winner of a content-identical sub-group is itself
    flagged for review: its duplicates are still deleted, but what remains
    cannot be told apart from the divergent copies by key alone, so a person
    has to look (brief §6.3 step 4).
    """
    mixed = len(group.kept) + len(group.needs_review) > 1
    kept_detail = "kept: winner of a content-identical sub-group" + (
        "; other events share its key but differ in content"
        if mixed
        else f", {len(group.to_delete)} copy/copies removed"
    )
    for winner in group.kept:
        plan.entries.append(
            _entry(by_href[winner.href], Action.NEEDS_REVIEW if mixed else Action.KEEP, kept_detail)
        )
    for member in group.needs_review:
        plan.entries.append(
            _entry(
                by_href[member.href],
                Action.NEEDS_REVIEW,
                "shares a key with other events but differs in content; nothing deleted",
            )
        )
    for member in group.to_delete:
        plan.entries.append(
            _entry(by_href[member.href], Action.DELETE, "byte-identical copy of the kept event")
        )
        plan.mutations.append(
            _Mutation(
                entry_index=len(plan.entries) - 1,
                resource=by_href[member.href],
                payload=None,
            )
        )


def _plan_delete_duplicates(resources: Sequence[CalendarResource]) -> _Plan:
    """Plan ``delete duplicates`` over the whole collection, per brief §6.

    Two phases, one helper each: :func:`_dedup_triage` sets aside what §6.1
    excludes, and :func:`_record_duplicate_group` turns each group that
    :func:`~arwen.dedup.group_duplicates` found — grouped, never compared
    pairwise — into report entries and deletions.
    """
    plan = _Plan(entries=[], mutations=[])
    by_href = {resource.href: resource for resource in resources}
    skipped, candidates = _dedup_triage(resources)
    plan.entries.extend(skipped)
    for group in group_duplicates(candidates):
        _record_duplicate_group(plan, group, by_href)
    return plan


def _execute_plan(connection: DavConnection, plan: _Plan) -> None:
    """Issue the planned mutations, per brief §7.

    Every request carries the ETag read during the scan as ``If-Match`` and
    ``Schedule-Reply: F``; both are :mod:`arwen.dav`'s job, and neither is
    optional. A ``412`` is a conflict, not a failure: the resource is
    skipped, recorded, and the run continues — never a blind retry without
    the ETag. Any other error is recorded the same way, so the report at
    the end describes exactly what happened to every resource.
    """
    for mutation in plan.mutations:
        resource = mutation.resource
        try:
            if mutation.payload is None:
                connection.delete_resource(resource.href, if_match=resource.etag)
            else:
                connection.put_resource(resource.href, mutation.payload, if_match=resource.etag)
        except PreconditionFailedError:
            plan.entries[mutation.entry_index] = replace(
                plan.entries[mutation.entry_index],
                action=Action.CONFLICT,
                detail="the resource changed on the server since the scan (412); skipped",
            )
        except DavError as exc:
            plan.entries[mutation.entry_index] = replace(
                plan.entries[mutation.entry_index],
                action=Action.FAILED,
                detail=str(exc),
            )


def _scan(
    connection: DavConnection, calendar_href: str, boundary: date | None, zone: tzinfo
) -> list[CalendarResource]:
    """Scan the selected calendar, narrowing server-side where brief §5 allows it.

    ``delete before`` narrows the scan with an RFC 4791 ``time-range``
    carrying only ``end`` — the interval is exclusive on the right, so an
    event starting exactly at the boundary is correctly left out.
    ``delete duplicates`` operates over the whole collection with no
    ``time-range`` at all (brief §6).
    """
    capabilities = connection.discover_capabilities(calendar_href)
    if not capabilities.calendar_access:
        _log.warning("%s did not advertise calendar-access in its DAV: header", calendar_href)
    if boundary is None:
        return connection.list_resources(calendar_href, capabilities)
    end = datetime.combine(boundary, time.min, tzinfo=zone)
    return connection.list_resources(calendar_href, capabilities, TimeRange(end=end))


def _run(args: argparse.Namespace, options: _Options, redaction: _RedactingFormatter) -> int:
    """Execute one parsed invocation and return its brief §7 exit code."""
    credentials = Credentials.from_file(options.env_file)
    redaction.redact(credentials.password)

    before = args.target == "before"
    boundary = parse_boundary(args.date) if before else None
    zone, zone_name = resolve_timezone(args.tz if before else None)
    now = datetime.now(UTC)

    connection = DavConnection(credentials)
    collection = select_calendar(connection, preselect=options.calendar)
    resources = _scan(connection, collection.href, boundary, zone)

    if boundary is not None:
        command = f"delete before {boundary.isoformat()}"
        plan = _plan_delete_before(resources, boundary, zone, now)
    else:
        command = "delete duplicates"
        plan = _plan_delete_duplicates(resources)

    if options.execute and plan.mutations:
        try:
            path = write_backup(
                options.backup_dir,
                collection.display_name,
                [mutation.resource for mutation in plan.mutations],
                now=now,
            )
        except BackupError as exc:
            print(f"arwen: {exc}", file=sys.stderr)
            print("arwen: aborting; no request was sent to the server.", file=sys.stderr)
            return EXIT_FINDINGS
        _log.info("backed up %d resource(s) to %s", len(plan.mutations), path)
        _execute_plan(connection, plan)

    report = Report(
        command=command,
        calendar=collection.display_name,
        timezone=zone_name,
        dry_run=not options.execute,
        examined=len(resources),
        entries=tuple(plan.entries),
    )
    sys.stdout.write(render(report, as_json=options.json_output))
    return EXIT_FINDINGS if report.has_findings() else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Run arwen and return its process exit code, per brief §7.

    Exit codes: :data:`EXIT_OK` clean; :data:`EXIT_FINDINGS` completed with
    skips, conflicts, or items needing review; :data:`EXIT_USAGE` usage
    error; :data:`EXIT_CONNECTION` connection or authentication failure;
    :data:`EXIT_INTERRUPTED` interrupted.

    Arguments:
        argv: The argument vector, excluding the program name. Defaults to
            :data:`sys.argv`.

    Returns:
        The exit code. Returned rather than raised, so the whole run is
        callable in-process from the test suite.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    options = _options_from(args)
    redaction = _configure_logging(verbose=options.verbose)

    try:
        return _run(args, options, redaction)
    except KeyboardInterrupt:
        print("arwen: interrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except (ConfigError, CalendarSelectionError, _UsageError) as exc:
        print(f"arwen: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except DavError as exc:
        print(f"arwen: {exc}", file=sys.stderr)
        return EXIT_CONNECTION
