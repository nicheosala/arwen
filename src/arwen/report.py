"""Human-readable and JSON report rendering.

Implements brief §8: one report per run, on stdout, in either a human form
or — under ``--json`` — a machine one. Both carry the same information and
the same shape in dry-run and execute mode, distinguished by an explicit
flag (:attr:`Report.dry_run`) rather than by which fields are present.

Nothing here performs I/O or reads the clock: :func:`render` returns a
string and the caller writes it. Nothing here decides an action either —
:class:`~arwen.model.Action` values arrive already decided by
:mod:`arwen.recurrence` and :mod:`arwen.dedup`.
"""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from arwen.model import Action

if TYPE_CHECKING:
    from arwen.model import EventTime

_ACTION_COLUMN = 18
_START_COLUMN = 26

_ATTENTION_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.SKIP_STRADDLING,
        Action.SKIP_UNPRUNABLE,
        Action.SKIP_RECURRING,
        Action.SKIP_NOT_EXAMINED,
        Action.NEEDS_REVIEW,
        Action.CONFLICT,
        Action.FAILED,
    }
)
"""Actions that make a run "completed with skips, conflicts, or items needing review".

Brief §7 gives those runs exit code 1. Everything else — a clean delete, a
clean modify, a kept duplicate winner, an untouched resource — leaves the
run at exit code 0.
"""


def format_event_time(value: EventTime) -> str:
    """Render a start or end for display, in a form that shows its RFC 5545 flavour.

    An :class:`~arwen.model.Instant` renders with its UTC offset, an
    :class:`~arwen.model.AllDayDate` as a bare ``YYYY-MM-DD``, and a
    floating date-time as a date-time with no offset at all — which is
    exactly what makes it floating (brief §5.1). Each flavour's ISO form
    already encodes that distinction, so no flavour tag is added on top.
    """
    return value.value.isoformat()


@dataclass(frozen=True, slots=True)
class ReportEntry:
    """One affected resource's line in the report, per brief §8.

    Attributes:
        href: The resource's href, so a line can be traced back to the
            server object it describes.
        uid: The ``UID`` of the resource's primary event, or ``""`` if it
            has none.
        summary: The event's ``SUMMARY``, unnormalized — the report shows
            what the calendar actually says.
        start: The event's start, pre-formatted by :func:`format_event_time`.
        action: What was done, or would be done, to the resource.
        detail: An optional one-line explanation — why a resource was
            skipped, which strategy pruned it, what the server replied.
    """

    href: str
    uid: str
    summary: str
    start: str
    action: Action
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReportCounts:
    """The brief §8 summary counts of one run.

    :attr:`skipped_recurring` covers both ways recurrence can take a
    resource out of play: a recurring resource excluded from duplicate
    detection (brief §6.1), and one that no pruning strategy could handle
    within the validation gate (brief §5.4 step 4).
    :attr:`skipped_not_examined` covers resources that were not eligible for
    any other reason — a ``VTODO``/``VJOURNAL``, or an event whose
    ``DTSTART`` could not be read.
    """

    examined: int
    to_delete: int
    to_modify: int
    skipped_straddling: int
    skipped_recurring: int
    skipped_not_examined: int
    needs_review: int
    conflicts: int
    failed: int


@dataclass(frozen=True, slots=True)
class Report:
    """The complete result of one run, ready to render, per brief §8.

    Attributes:
        command: The command line's effective subcommand, e.g.
            ``"delete before 2025-01-01"``.
        calendar: The selected calendar's display name.
        timezone: The resolved timezone's name. Brief §5.1 requires this be
            printed on every run, since it is what "before ``DATE``" means.
        dry_run: The explicit mode flag brief §8 requires. ``True`` means
            nothing was written; the rest of the report is unchanged either
            way.
        examined: How many resources the scan looked at.
        entries: One entry per *affected* resource, in plan order. A
            resource classified :attr:`~arwen.model.Action.UNTOUCHED` has
            no entry — it is counted in :attr:`examined` and nothing more.
    """

    command: str
    calendar: str
    timezone: str
    dry_run: bool
    examined: int
    entries: tuple[ReportEntry, ...]

    def _count(self, action: Action) -> int:
        """Count the entries carrying one action."""
        return sum(1 for entry in self.entries if entry.action is action)

    def counts(self) -> ReportCounts:
        """Summarize the entries into the brief §8 counts."""
        return ReportCounts(
            examined=self.examined,
            to_delete=self._count(Action.DELETE),
            to_modify=self._count(Action.MODIFY),
            skipped_straddling=self._count(Action.SKIP_STRADDLING),
            skipped_recurring=self._count(Action.SKIP_RECURRING)
            + self._count(Action.SKIP_UNPRUNABLE),
            skipped_not_examined=self._count(Action.SKIP_NOT_EXAMINED),
            needs_review=self._count(Action.NEEDS_REVIEW),
            conflicts=self._count(Action.CONFLICT),
            failed=self._count(Action.FAILED),
        )

    def has_findings(self) -> bool:
        """Report whether the run finished with skips, conflicts, or items needing review.

        This is exactly brief §7's exit-code-1 condition; the caller maps it
        onto the process exit status.
        """
        return any(entry.action in _ATTENTION_ACTIONS for entry in self.entries)


_COUNT_LABELS: tuple[tuple[str, str], ...] = (
    ("Examined", "examined"),
    ("To delete", "to_delete"),
    ("To modify", "to_modify"),
    ("Skipped (straddling)", "skipped_straddling"),
    ("Skipped (recurring)", "skipped_recurring"),
    ("Skipped (not examined)", "skipped_not_examined"),
    ("Needs review", "needs_review"),
    ("Conflicts", "conflicts"),
    ("Failed", "failed"),
)
"""Human labels for the §8 counts, paired with the :class:`ReportCounts` field they read."""


def _counts_mapping(counts: ReportCounts) -> dict[str, int]:
    """Return the counts as an ordered, JSON-ready mapping.

    Written out by hand rather than via ``dataclasses.asdict``, which would
    hand back a ``dict[str, Any]`` and lose the value type.
    """
    return {
        "examined": counts.examined,
        "to_delete": counts.to_delete,
        "to_modify": counts.to_modify,
        "skipped_straddling": counts.skipped_straddling,
        "skipped_recurring": counts.skipped_recurring,
        "skipped_not_examined": counts.skipped_not_examined,
        "needs_review": counts.needs_review,
        "conflicts": counts.conflicts,
        "failed": counts.failed,
    }


def render_human(report: Report) -> str:
    """Render the default, human-readable report of brief §8.

    The header states the command, the selected calendar, the resolved
    timezone (brief §5.1), and whether this was a dry run; then the counts;
    then one line per affected resource, giving its action, start, summary,
    and ``UID``.
    """
    mode = "DRY RUN — nothing was written" if report.dry_run else "EXECUTE — changes were written"
    lines = [
        f"arwen {report.command}",
        f"Mode:      {mode}",
        f"Calendar:  {report.calendar}",
        f"Timezone:  {report.timezone}",
        "",
    ]

    counts = report.counts()
    width = max(len(label) for label, _ in _COUNT_LABELS)
    lines.extend(
        f"{label + ':':<{width + 1}} {getattr(counts, field):>5}" for label, field in _COUNT_LABELS
    )
    lines.append("")

    if not report.entries:
        lines.append("No resources were affected.")
    else:
        for entry in report.entries:
            line = (
                f"{entry.action.value:<{_ACTION_COLUMN}} "
                f"{entry.start:<{_START_COLUMN}} "
                f"{entry.summary or '(no summary)'}  [{entry.uid or entry.href}]"
            )
            if entry.detail:
                line = f"{line}  — {entry.detail}"
            lines.append(line)

    if report.dry_run:
        lines.extend(["", "Dry run: no request that could change the server was issued."])
    return "\n".join(lines) + "\n"


def render_json(report: Report) -> str:
    """Render the ``--json`` report of brief §8.

    Carries the same information as :func:`render_human`, under stable
    keys, with the dry-run/execute distinction as an explicit boolean
    rather than a difference in shape. ``ensure_ascii`` is off so a
    non-ASCII ``SUMMARY`` stays readable.
    """
    payload = {
        "command": report.command,
        "dry_run": report.dry_run,
        "calendar": report.calendar,
        "timezone": report.timezone,
        "counts": _counts_mapping(report.counts()),
        "resources": [
            {
                "href": entry.href,
                "uid": entry.uid,
                "summary": entry.summary,
                "start": entry.start,
                "action": entry.action.value,
                "detail": entry.detail,
            }
            for entry in report.entries
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def render(report: Report, *, as_json: bool) -> str:
    """Render ``report`` in the form ``--json`` selected."""
    return render_json(report) if as_json else render_human(report)
