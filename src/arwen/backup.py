"""Pre-mutation ``.ics`` writer.

Writes every resource about to be deleted or modified to a single backup
file before any mutating request is sent (brief §7). The file is written,
flushed, and ``fsync``-ed before :func:`write_backup` returns, so a caller
that reaches the next statement knows the bytes are on disk; anything that
goes wrong on the way there is raised as :class:`BackupError`, which the
caller turns into "abort without touching the server".

The backup is an iCalendar *stream*: each resource's own ``VCALENDAR``
document, copied verbatim and concatenated. Merging every ``VEVENT`` into
one ``VCALENDAR`` would lose the resource boundaries a restore needs, so
each document keeps its own — annotated with :data:`HREF_PROPERTY` and
:data:`ETAG_PROPERTY` so a future ``restore`` knows where each one came
from and what it looked like when it was read.

Kept general enough for reuse by the future ``backup``/``restore`` commands
(brief §12): nothing here knows which command called it, or why.
"""

import os
import re
from datetime import UTC
from typing import TYPE_CHECKING

from icalendar import Calendar

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from pathlib import Path

    from arwen.dav import CalendarResource

_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_FALLBACK_SLUG = "calendar"

HREF_PROPERTY = "X-ARWEN-HREF"
"""Records the href each backed-up document was read from."""

ETAG_PROPERTY = "X-ARWEN-ETAG"
"""Records the ETag each backed-up document was read at, for a later ``If-Match``."""


class BackupError(Exception):
    """Raised when the backup file cannot be produced or written.

    Brief §7 makes this fatal *before* any request: a run that cannot save
    what it is about to destroy must not destroy it.
    """


def slugify(name: str) -> str:
    """Reduce a calendar display name to a filename-safe slug.

    Lower-cases, replaces every run of non-alphanumeric characters with a
    single hyphen, and trims leading/trailing hyphens. A name with nothing
    alphanumeric in it at all (or an empty one) yields ``"calendar"``, so
    the generated filename is never degenerate.
    """
    slug = _SLUG_PATTERN.sub("-", name.lower()).strip("-")
    return slug or _FALLBACK_SLUG


def backup_path(directory: Path, calendar_name: str, *, now: datetime) -> Path:
    """Build the brief §7 backup path: ``arwen-<calendar-slug>-<UTC-timestamp>.ics``.

    Arguments:
        directory: The ``--backup-dir`` the file belongs in.
        calendar_name: The selected calendar's display name, slugified into
            the filename by :func:`slugify`.
        now: The instant to stamp into the name, rendered in UTC. Passed in
            rather than read from the clock, so one run's backup name and
            its ``DTSTAMP``s agree.
    """
    stamp = now.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)
    return directory / f"arwen-{slugify(calendar_name)}-{stamp}.ics"


def _annotated_copy(resource: CalendarResource) -> Calendar:
    """Copy a resource's calendar and stamp its href and ETag onto the copy.

    The copy goes through a serialization round-trip, so unknown and ``X-``
    properties survive verbatim and the caller's own object is never
    mutated by the annotation.

    Raises:
        BackupError: If the resource does not serialize back to a single
            ``VCALENDAR``.
    """
    copied = Calendar.from_ical(resource.calendar.to_ical())
    if not isinstance(copied, Calendar):
        raise BackupError(f"{resource.href} does not serialize back as a single VCALENDAR")
    copied[HREF_PROPERTY] = resource.href
    copied[ETAG_PROPERTY] = resource.etag
    return copied


def render_backup(resources: Sequence[CalendarResource]) -> bytes:
    """Render resources into one iCalendar stream, ready to be written.

    Separated from :func:`write_backup` so the exact bytes that reach disk
    can be asserted in tests, and so a future ``backup`` command can reuse
    the rendering without the file handling.

    Raises:
        BackupError: If any resource cannot be serialized.
    """
    documents: list[bytes] = []
    for resource in resources:
        try:
            documents.append(_annotated_copy(resource).to_ical())
        except BackupError:
            raise
        except (ValueError, TypeError) as exc:
            raise BackupError(f"cannot serialize {resource.href}: {exc}") from exc
    return b"".join(documents)


def _fsync_directory(directory: Path) -> None:
    """Flush the directory entry of a freshly created file, where the platform allows it.

    Without this the file's *contents* are durable but its *name* may not
    be. Platforms that refuse to open a directory for reading (Windows)
    raise :class:`OSError`, which is ignored: the file itself is already
    synced, and brief §7's requirement is met either way.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)


def write_backup(
    directory: Path,
    calendar_name: str,
    resources: Sequence[CalendarResource],
    *,
    now: datetime,
) -> Path:
    """Write every resource about to be mutated to one ``.ics`` file, per brief §7.

    The directory is created if missing, the payload is rendered in full
    before the file is opened, and the file is flushed and ``fsync``-ed
    before this returns. Every failure — an unwritable directory, a full
    disk, a resource that will not serialize — surfaces as
    :class:`BackupError`, and the caller must then abort without issuing a
    single mutating request.

    Arguments:
        directory: The ``--backup-dir`` to write into (created if missing).
        calendar_name: The selected calendar's display name, used for the
            filename slug.
        resources: The resources as they were read during the scan — their
            pre-mutation content, which is what a restore would need.
        now: The instant to stamp into the filename.

    Returns:
        The path written.

    Raises:
        BackupError: If the backup cannot be rendered or written.
    """
    payload = render_backup(resources)
    path = backup_path(directory, calendar_name, now=now)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise BackupError(f"cannot write backup to {path}: {exc}") from exc
    _fsync_directory(directory)
    return path
