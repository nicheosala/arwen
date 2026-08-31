# arwen

Full spec: **`docs/brief.md`**. Read it before making any design decision —
this file only restates the invariants that must never be violated, no matter
which stage of the work is in progress.

## Non-negotiable invariants

- **Dry-run by default.** Nothing is ever written to the server unless
  `--execute` is passed. A dry run issues zero mutating HTTP requests.
- **`Schedule-Reply: F` on every mutating request.** Every `PUT` and `DELETE`
  carries it (RFC 6638). No iTIP scheduling message is ever emitted; attendees
  must never receive a cancellation or update. This is not a flag — there is
  no way to turn it off.
- **Backup before mutation.** On `--execute`, every resource that will be
  deleted or modified is written to the backup file, flushed and fsynced,
  _before the first mutating request_. If the backup cannot be written, abort
  without touching the server.
- **`If-Match` on every mutation.** Every `PUT` and `DELETE` carries the ETag
  read during the scan. A `412` is a conflict: skip that resource, record it,
  continue, and reflect it in the exit code — never a fatal error, never a
  blind retry without the ETag.
- **No dependencies beyond `caldav`, `icalendar`, `python-dateutil` and `recurring-ical-events`**
  at runtime. Everything else comes from the standard library. `pytest`,
  `ruff`, and `mypy` are dev dependencies only. Do not add HTTP clients, config
  libraries, CLI frameworks, or date parsers.
- **`recurring-ical-events` is for occurrence expansion only.** All pruning and
  mutation logic — `COUNT` → `UNTIL` conversion, `DTSTART` shift, `EXDATE`
  generation, override removal, before/after comparison — is hand-written in
  `recurrence.py`. The library never performs or decides a mutation, and it is
  never used as the oracle in a test that is meant to verify pruning.
- **No branching on server vendor, product, or version.** Write against the
  protocol (RFC 4791, RFC 5545, RFC 6638) only. Discover capabilities through
  `OPTIONS` and `supported-report-set`, never by detecting which server is on
  the other end.
- **`dav.py` is the only module that touches `caldav` directly.** It exposes
  fully typed domain objects to the rest of the code; `Any` must not leak into
  `recurrence.py`, `dedup.py`, or their callers. All calendar-data logic in
  `recurrence.py` and `dedup.py` is pure functions over `icalendar` objects —
  no I/O there. Calls into `recurring-ical-events` go through a single thin,
  typed expansion function.
- Target is Python 3.14 with PEP 649 deferred annotation evaluation. Do not add
  `from __future__ import annotations` — forward references work without it.

## §1.1 quality gates

The build is not done until all three exit cleanly, with zero errors and zero
warnings:

```bash
poetry run ruff check .
poetry run ruff format --check .
poetry run mypy .
```

- Full type annotations everywhere, including the test suite and the fake
  server — no untyped test helpers.
- mypy runs in strict mode over `src/` and `tests/`. Third-party stub gaps in
  `caldav`, `icalendar`, or `recurring-ical-events` are handled with narrowly
  scoped `[[tool.mypy.overrides]]` entries naming those packages, never by
  loosening the global config. Any surviving `# type: ignore` is narrowly coded
  (e.g. `# type: ignore[attr-defined]`, never bare) with a one-line comment
  explaining why.
- Ruff's lint `select` is the explicit, broad set defined in `pyproject.toml`
  per §1.1 — do not shrink it. New `ignore` entries need a comment justifying
  the conflict; never disable a rule just to make the gate pass.

Never loosen these gates to make code pass. Fix the code, or scope a narrow,
justified exception.
