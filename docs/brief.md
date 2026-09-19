# arwen — implementation brief

Build `arwen`, a command-line tool for maintaining CalDAV calendars.

This brief covers the **first iteration only**: the `delete` command. Backup and
restore commands are planned but explicitly out of scope. Design for them, do not
implement them.

---

## 1. Hard constraints

- **Python 3.14**, project managed with **uv**, `src/` layout.
- **Runtime dependencies: `caldav`, `icalendar`, `python-dateutil` and `recurring-ical-events`
  only.** Everything else must come from the standard library (`argparse`,
  `zoneinfo`, `hashlib`, `logging`, `datetime`, `http.server` for tests). Do not
  add HTTP clients, config libraries, CLI frameworks, or date parsers.
  `recurring-ical-events` is used for occurrence expansion only — see §5.4.
- **Everything in English**: code, comments, docstrings, README, CLI output,
  error messages, commit messages.
- **Dry-run is the default.** Nothing is ever written to the server unless
  `--execute` is passed.
- **The tool never emits iTIP scheduling messages.** Every mutating request
  (`PUT`, `DELETE`) carries `Schedule-Reply: F` (RFC 6638). Attendees of a
  deleted or modified event must never receive a cancellation or update. This is
  an invariant, not an option — there is no flag to turn it on.
- **Write against the protocol (RFC 4791, RFC 5545, RFC 6638), never against a
  specific server.** No branching on server product, version, or vendor quirks.
  Discover capabilities through `OPTIONS` and `supported-report-set`.

### 1.1 Typing and lint gates

All three gates are declared once, in `.pre-commit-config.yaml`, and CI runs
`pre-commit run --all-files` rather than restating them — the two can never
drift apart. The build is not done until all three exit cleanly, with **zero
errors and zero warnings**:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyrefly check --min-severity warn
```

**Full type annotations.** Every function, method, parameter, return value, and
module-level constant is annotated. This includes the test suite and the fake
server — no untyped test helpers.

**Pyrefly in strict mode**, covering `src/` and `tests/`:

```toml
[tool.pyrefly]
preset = "strict"
python-version = "3.14"
project-includes = ["src", "tests"]
search-path = [".", "src"]
```

`pyrefly check` exits 0 on warnings by default, so the gate always passes
`--min-severity warn`; that is what turns "zero warnings" into something the
build enforces rather than something a reader is asked to notice.

**Third-party stubs.** `caldav`, `icalendar`, `python-dateutil` and `recurring-ical-events` may
not ship complete type information. Do not paper over this by loosening the
global configuration and do not scatter `# type: ignore` through the codebase.
Instead:

- close a stub gap by adding the stub distribution as a dev dependency — as
  `types-python-dateutil` is — never by relaxing the global configuration;
- **isolate the untyped surface behind an adapter layer.** `dav.py` is the only
  module allowed to touch `caldav` directly, and it exposes fully typed domain
  objects to the rest of the code. `Any` must not leak into `recurrence.py`,
  `dedup.py`, or their callers;
- likewise, every call into `recurring-ical-events` goes through a single thin
  expansion function that returns a typed structure of your own. That function
  is the only place where a scoped override may apply — the pruning logic
  around it stays fully typed;
- every suppression that survives must be narrowly coded
  (`# pyrefly: ignore[bad-argument-type]`, never bare) and carry a one-line
  comment explaining why. Pyrefly reports the count of active suppressions on
  every run, so an obsolete one does not stay hidden.

Note that Pyrefly resolves imports from `search_path` first, then from its
bundled typeshed, and only then from site-packages. Typeshed removed its
`icalendar` stubs once the package started shipping `py.typed`, but Pyrefly
still bundles a copy of them, pinned to icalendar 6.x — so without help it
type-checks against stubs a major version behind the one installed here.
Listing the environment's site-packages in `search-path` puts the package's
own, current annotations first. That is the only reason the entry is there.

**Ruff must be configured with an explicit, broad rule selection** — the default
set is too small to be a meaningful gate. Start from:

```toml
[tool.ruff.lint]
select = ["E", "W", "F", "I", "N", "UP", "B", "A", "C4", "SIM", "PTH",
          "RET", "ARG", "TC", "ANN", "D", "RUF"]
```

`D` (pydocstyle) is included deliberately, since §11 requires docstrings on
every public function. If a rule genuinely conflicts with the design, add it to
`ignore` with a comment justifying it — do not silence it inline.

**Prefer expressing the invariants in the type system** where it is natural:
distinct types (or `NewType`) for a UTC instant versus a floating date-time
versus an all-day date, so §5.1 cannot be violated by accident; a frozen
dataclass for the duplicate key from §6.2; `Literal` or `enum` for the per-
resource action recorded in the report. Types that make the timezone mistakes
unrepresentable are worth more here than any amount of defensive checking.

---

## 2. Command surface

```
arwen delete before DATE [options]
arwen delete duplicates  [options]
```

Global options:

| Option | Meaning |
| --- | --- |
| `--execute` | Actually perform changes. Without it, the run is a dry run. |
| `--calendar NAME` | Preselect a calendar by display name (see §4). |
| `--env-file PATH` | Credentials file. Default: `./arwen.env`. |
| `--backup-dir PATH` | Default: `./backups`. |
| `--json` | Machine-readable report instead of the human one. |
| `--verbose` | Debug logging to stderr. |

`delete before` additionally takes `--tz IANA_NAME` (see §5).

`DATE` is an ISO calendar date, `YYYY-MM-DD`. No time component is accepted.

---

## 3. Credentials

Read from `arwen.env` (or `--env-file`). Expected keys:

```
ARWEN_CALDAV_URL=https://example.org
ARWEN_CALDAV_USERNAME=user@example.org
ARWEN_CALDAV_PASSWORD=secret
```

- Write a small stdlib parser: `KEY=VALUE`, `#` comments, optional surrounding
  quotes, blank lines ignored. Do not add a dotenv dependency.
- **The password must never appear in `argv`.** There is no `--password` option.
- **The password must never be logged**, including in verbose HTTP logs and in
  exception messages — redact it explicitly.
- Warn (do not abort) if the file's permissions are looser than `0600`.
- No other configuration comes from the environment. Everything else is a CLI
  flag.

---

## 4. Calendar selection

The user chooses the calendar **interactively on every run**. Server-assigned
collection identifiers are not assumed to be stable, so nothing is cached or
persisted between runs.

Procedure on each invocation:

1. Discover the current-user principal, then the calendar-home-set.
2. `PROPFIND` depth 1 for collections whose `supported-calendar-component-set`
   includes `VEVENT`.
3. Present a numbered list showing display name and href; read the selection
   from stdin.

`--calendar NAME` matches on display name and skips the prompt. If the name is
absent or ambiguous, fall back to the interactive list. If stdin is not a TTY and
no unambiguous `--calendar` was given, exit with a usage error rather than
guessing.

**Capability discovery:** issue `OPTIONS` and check the `DAV:` header for
`calendar-access`; read `supported-report-set` to decide whether
`calendar-query` with `time-range` is available. If it is not, fall back to
`PROPFIND` plus client-side filtering. Both paths must produce identical results
— add a test that runs the same fixture set through both.

---

## 5. `arwen delete before DATE`

Removes what is already over. The interval is open on the left and exclusive on
the right: everything strictly before `DATE` at 00:00:00 in the resolved
timezone. Per RFC 4791 a `time-range` may carry only `end`, so the query stays
server-side where supported (converted to UTC as the spec requires).

### 5.1 Timezone resolution

- `--tz` accepts an IANA name; the default is the machine's local zone.
- **Always print the resolved zone in the report.** UTC is deliberately not the
  default: an event on 1 February at 00:30 local is 31 January at 23:30 UTC and
  would be swept up by "delete everything before February".
- Date-times with `TZID` or trailing `Z` are compared as absolute instants.
- **Floating** date-times (no `TZID`, no `Z`) are interpreted in the resolved
  zone.
- **All-day** events (`VALUE=DATE`) are compared as pure dates, with no timezone
  conversion. Remember that `DTEND` for an all-day event is **exclusive**.

### 5.2 Effective end of an event

In order: `DTEND`; else `DTSTART` + `DURATION`; else, per RFC 5545, `DTSTART`
for a timed event and `DTSTART` + 1 day for an all-day event.

### 5.3 Non-recurring events

| Case | Action |
| --- | --- |
| Ends entirely before `DATE` | Delete the resource. |
| Starts before `DATE`, ends on or after it | **Skip**, warn, list in the report. |
| Starts on or after `DATE` | Untouched. |

An event that straddles the boundary is not old, it is in progress. **Never
truncate it.** Truncation would misrepresent the event and would additionally
require recomputing `DURATION` and handling exclusive all-day `DTEND` — all
avoided by skipping.

### 5.4 Recurring events

A resource counts as recurring if **any** of its components carries `RRULE`,
`RDATE`, or `RECURRENCE-ID`. Inspect all components, not just the first
`VEVENT`.

Goal: drop only the occurrences that are entirely before `DATE`, keep the rest,
and write the result back with a **single `PUT`** per resource.

**Division of labour.** Use `recurring-ical-events` for **occurrence expansion
only** — both to find the occurrences to remove in step 1 and to expand the
pruned result for the validation gate in step 4. Everything else stays
hand-written in `recurrence.py`: the `COUNT` → `UNTIL` conversion, the `DTSTART`
shift, `EXDATE` generation, override removal, and the before/after comparison.
The library must never perform, or decide, any mutation of the calendar data.

1. Expand occurrences. If every occurrence ends before `DATE`, delete the whole
   resource. If none is affected, leave it untouched.
2. Otherwise prune, using **`DTSTART`-shift as the primary strategy**:
   - Move `DTSTART` to the first surviving occurrence, preserving the event's
     duration (adjust `DTEND`, or keep `DURATION` as-is).
   - **`COUNT` must first be converted to `UNTIL`**, computed from the
     *original* `DTSTART`. `COUNT` is relative to `DTSTART`, so shifting it
     without this conversion invents future occurrences that never existed.
     This is the single most dangerous bug in this command.
   - Drop `RDATE` values before `DATE`; drop now-redundant `EXDATE` values
     before `DATE`; drop `RECURRENCE-ID` override components whose instance is
     entirely before `DATE`.
   - `UNTIL` on the right-hand side is left alone — it is not the boundary this
     command touches.
3. An occurrence that straddles the boundary **survives**, consistent with
   §5.3. Note the documented asymmetry: a straddling occurrence of a series is
   kept whole; it is never trimmed by synthesising a `RECURRENCE-ID` override.
4. **Validation gate.** Expand the pruned resource and compare the resulting set
   of surviving occurrences (start instants plus overrides) against the set
   expected from the original. If they differ in any respect, discard the
   shifted version and fall back to **`EXDATE`-only pruning**: leave `DTSTART`
   untouched and add one `EXDATE` per removed occurrence. If the `EXDATE`
   version also fails validation, skip the resource and report it. Never write
   an unvalidated result.
5. On a successful prune, bump `SEQUENCE` and refresh `DTSTAMP` /
   `LAST-MODIFIED`. Preserve all other properties verbatim, including unknown
   and `X-` properties.
6. **Unbounded series** (no `COUNT`, no `UNTIL`): bound the removal expansion at
   `DATE`; for the validation comparison, use a window from `DATE` to
   `DATE + N years` where `N` is a documented module constant.

This pruning engine is the core of the tool. Give it the densest test coverage.

---

## 6. `arwen delete duplicates`

Operates over the whole collection with a `PROPFIND` — **no `time-range` query**,
no `--before` filter.

### 6.1 What is examined

Skipped and reported as "not examined":

- any resource containing `RRULE`, `RDATE`, or a component with
  `RECURRENCE-ID` (recurring de-duplication is deferred to a later iteration);
- non-`VEVENT` resources (`VTODO`, `VJOURNAL`).

### 6.2 Duplicate key

`(normalized SUMMARY, start instant, end instant, all-day flag)`

- `SUMMARY`: trimmed, internal whitespace collapsed, **case-sensitive**. A
  missing `SUMMARY` normalizes to the empty string and remains eligible.
- Start and end are compared as **absolute instants** (normalized to UTC) for
  timed events, and as pure dates for all-day events. Two `DTSTART`s with
  different `TZID`s denoting the same moment must match.
- **Timed and all-day events never match each other**, even on the same date.
- End is derived exactly as in §5.2.

### 6.3 Grouping algorithm

Compare by grouping, **never pairwise**. Pairwise comparison is the classic bug
that leaves two survivors out of five copies.

1. Group all resources into `key -> [resources]`.
2. Within each key group, sub-group by **content hash**: a canonical
   serialization of the `VEVENT` with volatile properties excluded — `UID`,
   `DTSTAMP`, `LAST-MODIFIED`, `CREATED`, `SEQUENCE`, `PRODID`, plus href and
   ETag. Normalize property order and parameter order before hashing. Document
   the exclusion list in the README.

   **Scope of "the `VEVENT`".** The hash covers each `VEVENT` component and
   recurses into that component's *own* children — a `VALARM`'s `ACTION`,
   `DESCRIPTION`, and `TRIGGER` are part of the event's identity, per step 4
   below, as is the presence or absence of the alarm itself. It never
   ascends to the enclosing `VCALENDAR`: sibling components — `VTIMEZONE`
   above all — and calendar-level properties do not contribute. A `VTIMEZONE`
   is a timezone *definition* shipped alongside the event, not event content,
   and two clients exporting the same instant emit entirely different
   transition tables for the same `TZID` (Thunderbird writes `Europe/Rome` as
   49 `STANDARD`/`DAYLIGHT` subcomponents reaching back to 1893; DAVx5 writes
   two modern rules). Hashing those would make one event look like two,
   exactly on a calendar synced by more than one client — the case where
   de-duplication matters most.

   This costs nothing in strictness: a `DTSTART` or `DTEND` carries its `TZID`
   as a **parameter**, and parameters are part of every canonical line, so an
   event genuinely scheduled in a different zone still hashes differently.
3. For each content-identical sub-group of *N* resources (any *N* ≥ 2), keep one
   winner and delete the remaining *N* − 1.
4. **Mixed sub-group rule:** if a key group contains more than one distinct
   content sub-group, reduce each content-identical sub-group of size ≥ 2 to
   a single winner as in step 3, and list everything that remains — those
   winners and every singleton — as "needs manual review". Nothing outside a
   content-identical sub-group is ever deleted, and a group in this state
   still yields deletions. Two events with the same key but different
   `DESCRIPTION`, `LOCATION`, `ATTENDEE`, or `VALARM` are not interchangeable,
   and deleting either loses data.

**Winner tie-break**, applied in order, so results never depend on the order the
server returned resources in:

1. highest `SEQUENCE` (RFC 5545 revision number);
2. most recent `LAST-MODIFIED`;
3. most recent `DTSTAMP`;
4. lexicographically smallest `UID`.

**Post-condition to assert in tests:** for every key group that was acted upon,
**exactly one** resource survives — for *N* = 2, *N* = 5, and *N* = 20 alike.

---

## 7. Safety model

- **Automatic backup before any mutation.** On `--execute`, write every resource
  that will be deleted or modified to a single `.ics` file in `--backup-dir`
  (default `./backups`, created if missing), named
  `arwen-<calendar-slug>-<UTC-timestamp>.ics`. The file must be written,
  flushed, and fsynced **before the first mutating request**. If the backup
  cannot be written, abort without touching the server.
- Dry runs write no backup, because they mutate nothing.
- **Optimistic concurrency:** every `PUT` and `DELETE` carries `If-Match` with
  the ETag read during the scan. On `412`, skip that resource, record a
  conflict, and continue.
- **Continue on error**, then print an aggregated report. Never leave the
  calendar half-processed without telling the user exactly what happened.
- Exit codes: `0` clean; `1` completed with skips, conflicts, or items needing
  review; `2` usage error; `3` connection or authentication failure; `130`
  interrupted.

---

## 8. Output

Human-readable report on stdout by default, `--json` for the machine form. Both
modes must carry the same information and the same shape in dry-run and execute
mode, distinguished by an explicit flag in the output.

The report includes: selected calendar, resolved timezone, and counts for
examined / to delete / to modify / skipped-straddling / skipped-recurring /
needs-review / conflicts. Then one line per affected resource with UID, summary,
start, and the action taken (or that would be taken).

Logging goes to stderr under `--verbose`. Credentials never appear in it.

---

## 9. Project layout

uv with a `src/` layout, roughly:

```
src/arwen/
  cli.py          argparse subparsers, exit codes
  config.py       arwen.env parsing, redaction
  discovery.py    principal, calendar-home-set, capabilities, interactive picker
  dav.py          thin wrapper over caldav: If-Match, Schedule-Reply, ETags
  recurrence.py   pruning engine — pure functions
  dedup.py        key, content hash, grouping, tie-break — pure functions
  backup.py       pre-mutation .ics writer
  report.py       human and JSON reports
tests/
  fixtures/       .ics corpus
  fake_server.py  in-process CalDAV server
```

**Design rule:** all calendar-data logic lives in pure functions that take
`icalendar` objects and return `icalendar` objects. No module under
`recurrence.py` or `dedup.py` performs I/O. This is what makes the hard parts
testable without a server.

---

## 10. Tests

pytest, in three layers. Layers 1 and 2 must run offline in CI with no
credentials and no network.

### Layer 1 — unit tests on the pure functions

Against a fixture corpus in `tests/fixtures/`. For every pruning fixture, assert
the expansion of the pruned resource against an **explicitly written expected
list of instants**, not merely against another call to the same expander.
`recurring-ical-events` is the tool under use, not the oracle; a test that
compares the library to itself proves nothing about the pruning.

Required pathological cases:

- all-day event whose exclusive `DTEND` makes a naive implementation off by one
  day (both commands);
- `DURATION` instead of `DTEND`; `DTEND` missing entirely;
- floating date-times; `DTSTART` with `TZID` versus the equivalent UTC value
  (must be recognised as the same instant);
- `RRULE` with `COUNT` (the shift-without-conversion trap);
- `RRULE` with `UNTIL`; `BYSETPOS`; explicit `RDATE`; pre-existing `EXDATE`;
- `RECURRENCE-ID` overrides on both sides of the boundary;
- an orphan `RECURRENCE-ID` override with no master component;
- an unbounded weekly series starting in 2015;
- a boundary that falls on a DST transition;
- line folding, escaped characters, and non-ASCII summaries;
- **5 byte-identical copies** of one event (must reduce to exactly 1);
- **3 identical copies plus 2 divergent ones** (must reduce to 3 survivors — the
  winner of the identical sub-group plus the two divergent ones — with all
  three flagged for review and the other two identical copies deleted);
- two events with an identical key but different `RRULE` (must not be touched).

### Layer 2 — in-process fake CalDAV server

A `http.server`-based fake implementing enough `PROPFIND`, `REPORT`, `PUT`, and
`DELETE` with real ETag and `If-Match` semantics. It must offer:

- a mode that returns collections and resources in **shuffled order**, to prove
  the tie-break is deterministic;
- a mode that returns `412` on a `PUT`, to prove conflict handling;
- a mode that advertises no `calendar-query` support, to exercise the
  client-side filtering fallback;
- **request recording**, so tests can assert on the requests actually issued.

**Do not test the implementation against a mock of itself.** Assertions belong on
the resulting iCalendar data and on the HTTP requests the fake server observed —
not on internal call counts.

### Layer 3 — optional integration tests

Against a real server, skipped unless `ARWEN_IT_*` variables are set. Never
required for a green build.

### Property-style checks worth encoding

- Pruning never changes any surviving occurrence.
- After `delete duplicates`, every acted-upon key has exactly one survivor.
- No operation ever increases the number of resources in the collection.
- **A dry run issues zero mutating HTTP requests** — assert this against the
  fake server's request log for both commands.
- No request ever omits `Schedule-Reply: F`.

---

## 11. Documentation

`README.md` covers: installation, the `arwen.env` format, usage for both
commands, the safety model, and an explicit statement that **no scheduling
messages are ever sent to attendees**.

Document the deliberate limitations, so they read as decisions rather than gaps:

- events straddling the boundary are skipped, never truncated;
- recurring events are excluded from duplicate detection in this version;
- how exclusive all-day `DTEND` is handled;
- the expansion window used for unbounded series;
- the property exclusion list used for the content hash.

Docstrings on every public function.

---

## 12. Out of scope — do not implement

- `backup` and `restore` commands. Keep `backup.py` general enough to be reused
  by them later, but expose no CLI surface for them now.
- Duplicate detection among recurring events.
- Any `--from` / range-with-lower-bound variant of `delete before`.
- Dependencies beyond `caldav`, `icalendar`, `python-dateutil` and `recurring-ical-events` (dev
  dependencies aside).
- Using `recurring-ical-events` for anything other than reading out occurrences.
- Persisting calendar ids or hrefs between runs.
- Interactive confirmation prompts other than calendar selection.
- Any code path that branches on which server is on the other end.
- Loosening the global Pyrefly or Ruff configuration to make the gates in §1.1
  pass. Fix the code, or scope the exception to the offending third-party
  module and say why.
