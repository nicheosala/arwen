# arwen

A command-line tool for maintaining CalDAV calendars.

`arwen` removes what a calendar no longer needs — events that are already
over, and duplicate copies of the same event — with a safety model built
around one idea: **it must never be able to surprise you**. It is a dry run
unless you say otherwise, it backs up everything it is about to touch before
it touches any of it, and it never sends a scheduling message to anyone.

---

## Installation

Requires **Python 3.14** and [Poetry](https://python-poetry.org/).

```bash
git clone <this repository>
cd arwen
poetry install
```

That gives you the `arwen` command inside the project environment:

```bash
poetry run arwen delete before 2025-01-01
```

Runtime dependencies are `caldav`, `icalendar`, `python-dateutil` and
`recurring-ical-events`, and nothing else — everything else comes from the
standard library.

---

## Credentials: `arwen.env`

Connection details are read from a file, by default `./arwen.env`, or from
whatever `--env-file PATH` names:

```ini
# Lines starting with # are comments; blank lines are ignored.
ARWEN_CALDAV_URL=https://caldav.example.org
ARWEN_CALDAV_USERNAME=user@example.org
ARWEN_CALDAV_PASSWORD="a value may be quoted"
```

All three keys are required. Values may be wrapped in one layer of matching
single or double quotes, which is stripped.

**The password is never accepted on the command line.** There is no
`--password` option, so it never appears in `argv`, in your shell history, or
in the process list. It is also redacted from every log line, including the
`--verbose` HTTP logs.

`arwen` warns — but does not refuse to run — if the file is readable or
writable by anyone other than its owner. `chmod 600 arwen.env`.

---

## Usage

```
arwen delete before DATE [options]
arwen delete duplicates  [options]
```

| Option | Meaning |
| --- | --- |
| `--execute` | Actually perform changes. Without it, the run is a dry run. |
| `--calendar NAME` | Preselect a calendar by display name. |
| `--env-file PATH` | Credentials file. Default: `./arwen.env`. |
| `--backup-dir PATH` | Where pre-mutation backups are written. Default: `./backups`. |
| `--json` | Machine-readable report instead of the human one. |
| `--verbose` | Debug logging to stderr. |

`delete before` additionally takes `--tz IANA_NAME`.

### Choosing a calendar

The calendar is chosen **on every run**. Server-assigned collection
identifiers are not assumed to be stable, so nothing is cached or persisted
between runs. `arwen` discovers the current-user principal, then the
calendar-home-set, then lists every collection that supports `VEVENT`, and
prompts:

```
1. Personal (/calendars/user/personal/)
2. Work (/calendars/user/work/)
Select a calendar:
```

`--calendar NAME` matches on display name and skips the prompt. If that name
matches nothing, or matches more than one calendar, the prompt appears
anyway — and if stdin is not a terminal, `arwen` exits with a usage error
rather than guessing which calendar you meant.

### `arwen delete before DATE`

Removes what is already over. `DATE` is an ISO calendar date, `YYYY-MM-DD`;
no time component is accepted. The interval is exclusive on the right:
everything strictly before `DATE` at 00:00:00 in the resolved timezone.

```bash
poetry run arwen delete before 2025-01-01 --calendar Personal --tz Europe/Rome
poetry run arwen delete before 2025-01-01 --calendar Personal --tz Europe/Rome --execute
```

**The timezone matters, and is always printed.** UTC is deliberately not the
default: an event on 1 February at 00:30 local is 31 January at 23:30 UTC, and
would be swept up by "delete everything before February" if the boundary were
resolved in the wrong zone. `--tz` takes an IANA name; without it, `arwen`
resolves the machine's local zone (via `TZ`, `/etc/timezone`, or
`/etc/localtime`) to a real IANA zone, so a boundary in another season still
gets that season's UTC offset.

Within that zone:

- date-times with a `TZID` or a trailing `Z` are compared as absolute instants;
- **floating** date-times (no `TZID`, no `Z`) are interpreted in the resolved
  zone;
- **all-day** events (`VALUE=DATE`) are compared as pure dates, with no
  timezone conversion at all.

Non-recurring events are deleted if they ended before `DATE`, skipped if they
straddle it, and left alone if they start on or after it.

Recurring events are **pruned**, not deleted, unless every occurrence is over.
`arwen` moves `DTSTART` to the first surviving occurrence, converts `COUNT` to
an equivalent `UNTIL` computed from the *original* `DTSTART`, drops `RDATE`
and now-unreachable `EXDATE` values before the boundary, removes override
components whose instance is gone, bumps `SEQUENCE`, and writes the result
back with a single `PUT`.

Every pruned resource then goes through a **validation gate**: it is expanded
again, and the surviving occurrences must match the original's exactly. If
they do not, `arwen` discards that result and retries with `EXDATE`-only
pruning, which leaves `DTSTART` and the `RRULE` untouched. If that also fails
validation, the resource is skipped and reported. **An unvalidated result is
never written.**

### `arwen delete duplicates`

Removes redundant copies of the same event across the whole collection.

```bash
poetry run arwen delete duplicates --calendar Personal
poetry run arwen delete duplicates --calendar Personal --execute
```

Two events are candidates for de-duplication when they share a key of
`(normalized SUMMARY, start instant, end instant, all-day flag)`. The summary
is trimmed and its internal whitespace collapsed, but comparison stays
**case-sensitive**; a missing summary normalizes to the empty string and
remains eligible. Starts and ends are compared as absolute instants, so two
`DTSTART`s written with different `TZID`s but naming the same moment do match
— while a timed and an all-day event never match each other, even on the same
date.

Sharing a key is not enough to be deleted. Within each key group, `arwen`
sub-groups by a **content hash**, and only deletes copies that are identical
in content. Comparison is always by group, never pairwise: five copies of one
event reduce to exactly one survivor, not the two that pairwise comparison
famously leaves behind.

The survivor of each identical sub-group is chosen deterministically, so the
result never depends on the order the server happened to return resources in:

1. highest `SEQUENCE`;
2. most recent `LAST-MODIFIED`;
3. most recent `DTSTAMP`;
4. lexicographically smallest `UID`.

If a key group holds more than one *distinct* content sub-group — say three
identical copies and two that differ in `LOCATION` — then the two redundant
identical copies are still deleted, but everything that remains is flagged
**needs review** rather than reduced further. Events that differ in
`DESCRIPTION`, `LOCATION`, `ATTENDEE`, or `VALARM` are not interchangeable,
and deleting either one would lose data.

---

## The report

Both commands print a report on stdout. `--json` swaps the human form for a
machine one; both carry the same information and the same shape in dry-run
and execute mode, distinguished by an explicit flag.

```
arwen delete before 2025-01-01
Mode:      DRY RUN — nothing was written
Calendar:  Personal
Timezone:  Europe/Rome

Examined:                   4
To delete:                  1
To modify:                  1
Skipped (straddling):       1
Skipped (recurring):        0
Skipped (not examined):     0
Needs review:               0
Conflicts:                  0
Failed:                     0

delete             2024-01-01T09:00:00+00:00  Past  [past@example.org]
modify             2024-01-01T09:00:00+00:00  Weekly stand-up  [weekly@example.org]  — drops 53 occurrence(s), keeps 51, via dtstart-shift
skip-straddling    2024-12-30T09:00:00+00:00  Straddling  [straddling@example.org]  — starts before DATE but is still in progress; not truncated

Dry run: no request that could change the server was issued.
```

Logging goes to stderr under `--verbose`, never to stdout, and never contains
the password.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Clean. |
| `1` | Completed with skips, conflicts, or items needing review. Also used when a backup could not be written and the run stopped before its first request. |
| `2` | Usage error — a malformed argument, an unreadable credentials file, or no unambiguously selectable calendar. |
| `3` | Connection or authentication failure. |
| `130` | Interrupted. |

---

## Safety model

**Dry-run by default.** Nothing is written to the server unless `--execute` is
passed. A dry run issues **zero** mutating HTTP requests — not requests that
are sent and ignored, but none at all. This is asserted in the test suite
against the requests an in-process CalDAV server actually observed.

**No scheduling messages are ever sent to attendees.** Every mutating request
carries `Schedule-Reply: F` (RFC 6638). Deleting or modifying an event with
attendees never sends them a cancellation, an update, or any other iTIP
message. **This is not a flag.** There is no option to turn it on, and no
code path that omits the header.

**Backup before mutation.** On `--execute`, every resource that will be
deleted or modified is written to a single `.ics` file in `--backup-dir`
(default `./backups`, created if missing), named
`arwen-<calendar-slug>-<UTC-timestamp>.ics`. The file is written, flushed,
and `fsync`-ed **before the first mutating request**. If the backup cannot be
written — an unwritable directory, a full disk — `arwen` aborts without
touching the server at all.

The backup is an iCalendar stream: each resource's own `VCALENDAR` document,
copied verbatim, with two annotations added at the document level recording
where it came from:

```
X-ARWEN-HREF:/calendars/user/personal/event.ics
X-ARWEN-ETAG:"a1b2c3d4"
```

Dry runs write no backup, because they mutate nothing.

**Optimistic concurrency.** Every `PUT` and `DELETE` carries `If-Match` with
the ETag read during the scan. A `412` means the resource changed on the
server since `arwen` looked at it: that resource is skipped, recorded as a
conflict, and the run continues. There is never a blind retry without the
ETag.

**Continue on error.** A failure on one resource does not abandon the rest.
The report at the end says exactly what happened to every resource, and the
exit code reflects it.

**No server-specific behaviour.** `arwen` is written against RFC 4791, RFC
5545, and RFC 6638. It discovers what a server can do through `OPTIONS` and
`supported-report-set` — never by detecting which product is on the other
end. Where a server does not support a `calendar-query` `REPORT` with a
`time-range` filter, `arwen` falls back to `PROPFIND` and filters
client-side; both paths produce identical results.

The same rule governs how event bodies are read. `calendar-data` is a REPORT
property (RFC 4791 §9.6), so a server is free to answer a `PROPFIND` without
it. `arwen` asks for it anyway — a server that supplies it saves a round trip
— but never assumes it arrived: any resource listed without a body is
fetched with a `calendar-multiget` `REPORT`, or a per-resource `GET` where
that report is unavailable. Properties are read only from `propstat`
elements whose status is 2xx, so a server that *names* a property it could
not supply is never mistaken for one supplying empty content.

---

## Deliberate limitations

These are decisions, not gaps.

**Events straddling the boundary are skipped, never truncated.** An event that
starts before `DATE` and ends on or after it is not old — it is in progress.
Truncating it would misrepresent what happened, and would additionally require
recomputing `DURATION` and handling the exclusive all-day `DTEND`. `arwen`
leaves it whole, warns, and lists it in the report.

The same holds inside a series: an occurrence that straddles the boundary
survives whole. It is never trimmed by synthesising a `RECURRENCE-ID`
override. This is a deliberate asymmetry — a straddling *occurrence* is kept
in a resource that is otherwise pruned — and it exists so that pruning can
never invent a component the calendar did not have.

**Recurring events are excluded from duplicate detection.** Any resource
carrying `RRULE`, `RDATE`, or a component with `RECURRENCE-ID` is reported as
"not examined" by `delete duplicates` and is never grouped, never deleted, and
never flagged — however its key happens to compare. Two series with the same
summary and start can still differ in every occurrence after the first, and
comparing them properly is a different problem from comparing two single
events. It is deferred to a later version. Non-`VEVENT` resources (`VTODO`,
`VJOURNAL`) are likewise reported as not examined.

**All-day `DTEND` is exclusive, and treated as such.** An all-day event with
`DTSTART;VALUE=DATE:20241231` and `DTEND;VALUE=DATE:20250101` occupies 31
December only — the `DTEND` is the first day *not* covered. It is therefore
deleted by `delete before 2025-01-01`, not treated as straddling the
boundary. When an all-day event has no `DTEND` at all, its effective end is
`DTSTART` + one day, per RFC 5545. Effective ends are derived in one place,
in this order: `DTEND`; else `DTSTART` + `DURATION`; else `DTSTART` for a
timed event and `DTSTART` + 1 day for an all-day one.

**Unbounded series are validated over a ten-year window.** A series with
neither `COUNT` nor `UNTIL` has no last occurrence, so the validation gate
cannot compare the original and pruned expansions exhaustively. It compares
them over a finite window instead, running to `DATE` plus
`arwen.recurrence.VALIDATION_WINDOW_YEARS` — currently **10 years**. A series
whose surviving occurrences all fall beyond that window is reported as
unprunable rather than pruned on incomplete evidence.

**The content hash excludes volatile properties.** Two copies of an event are
"identical in content" when their canonical serializations match with these
properties removed:

| Excluded | Why |
| --- | --- |
| `UID` | Two copies of the same event on a server necessarily differ here. |
| `DTSTAMP` | Records when the object was created or last sent, not what it says. |
| `LAST-MODIFIED` | Same. |
| `CREATED` | Same. |
| `SEQUENCE` | A revision number, not content. |
| `PRODID` | Identifies the software that wrote the file. |
| href | Not part of the calendar data at all. |
| ETag | Same. |

Everything else is part of the identity, recursively — including
`DESCRIPTION`, `LOCATION`, every `ATTENDEE`, and any `VALARM` subcomponent.
Property order and parameter order are normalized before hashing, so two
files that differ only in the order they wrote things still hash the same.

**Not implemented in this version:** `backup` and `restore` commands (the
backup *writer* exists and is used, but has no CLI surface of its own),
de-duplication among recurring events, and any `--from` / lower-bound variant
of `delete before`.

---

## Development

The build is not done until all three of these exit cleanly, with zero errors
and zero warnings:

```bash
poetry run ruff check .
poetry run ruff format --check .
poetry run mypy .
```

The test suite runs offline, with no credentials and no network:

```bash
poetry run pytest
```

It has two layers: unit tests over the pure pruning and de-duplication
functions against a fixture corpus in `tests/fixtures/`, and integration tests
against an in-process fake CalDAV server (`tests/fake_server.py`) that
implements real ETag and `If-Match` semantics and records every request it
receives. Assertions live on the resulting iCalendar data and on those
recorded requests — never on internal call counts.
