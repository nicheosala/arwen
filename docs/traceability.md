# Brief → test traceability

Every normative statement in [`brief.md`](brief.md), and the test that proves
it. One row per requirement, in brief order.

This file exists because a requirement can have an invariant, a docstring
asserting the invariant, and a test named after the invariant, and still not be
proven — the §3 redaction rule was in exactly that state, with a test that
passed because the code path it named was never reached. The question this
table answers is not "is there a test for §N" but "would that test fail if §N
were violated".

Status is one of:

| | |
| --- | --- |
| **proven** | A test fails if the requirement is violated. |
| **partial** | A test covers part of the requirement, or covers it only on one path. |
| **unproven** | Nothing would fail. Enforced by construction, by review, or not at all. |

Keep it current: a new requirement gets a row before it gets an implementation,
and a row that says *unproven* is a to-do, not a note.

---

## §1 Hard constraints

| Requirement | Proven by | Status |
| --- | --- | --- |
| Runtime dependencies are `caldav`, `icalendar`, `python-dateutil`, `recurring-ical-events` and nothing else | `test_project.py::test_the_runtime_dependency_set_is_exactly_the_four_the_brief_allows` | proven |
| Dry run is the default; nothing is written without `--execute` | `test_cli.py::TestDryRunIssuesNoMutations` (both commands) | proven |
| Every mutating request carries `Schedule-Reply: F` | `test_cli.py::TestScheduleReplyInvariant`, `test_dav.py::TestMutations::test_put_and_delete_carry_if_match_and_schedule_reply` | proven |
| No branching on server product, version or vendor | — | **unproven** |
| Capabilities discovered via `OPTIONS` and `supported-report-set` | `test_dav.py::TestCapabilityDiscovery`, `test_fake_server.py::TestCapabilityDiscovery` | proven |
| `recurring-ical-events` is used for expansion only, never to decide a mutation | — | **unproven** |
| Everything in English | — | unproven (not mechanically checkable) |

§1.1's gates are not tests; they are the hooks in `.pre-commit-config.yaml`,
run by CI. `pyproject.toml` is their only configuration.

## §2 Command surface

| Requirement | Proven by | Status |
| --- | --- | --- |
| `--execute` performs changes | `test_cli.py::TestDeleteBeforeExecute`, `::TestDeleteDuplicatesExecute` | proven |
| `--calendar NAME` preselects by display name | `test_discovery.py::TestSelectCalendarPreselect` | proven |
| `--env-file PATH` | every `test_cli.py` case passes one | proven |
| `--backup-dir PATH` | `test_cli.py::TestBackup` | proven |
| `--json` selects the machine report | `test_report.py::TestRenderSelector`, `test_cli.py::TestReportShape::test_json_keys_are_identical_in_both_modes` | proven |
| `--verbose` sends debug logging to stderr | `test_cli.py::TestPasswordRedaction` (asserts on the stderr handler's output) | proven |
| `--tz` belongs to `delete before` only | `test_cli.py::TestCommandSurface::test_tz_is_rejected_on_delete_duplicates`, `::test_tz_is_accepted_on_delete_before` | proven |
| `DATE` is `YYYY-MM-DD`, no time component | `test_cli.py::TestExitCodes::test_a_date_with_a_time_component_is_a_usage_error`, `::test_a_non_iso_date_is_a_usage_error` | proven |

## §3 Credentials

| Requirement | Proven by | Status |
| --- | --- | --- |
| `KEY=VALUE` parsing: comments, blank lines, one layer of quotes | `test_config.py::TestParsing` (7 cases) | proven |
| The password never appears in `argv`; there is no `--password` option | `test_cli.py::TestCommandSurface::test_no_parser_anywhere_offers_a_password_option`, `::test_a_password_option_is_rejected_at_parse_time` | proven |
| The password never appears in a repr | `test_config.py::TestRedaction::test_repr_omits_password` | proven |
| The password is never logged, including verbose HTTP logs | `test_cli.py::TestPasswordRedaction::test_records_from_a_third_party_logger_are_redacted`, `::test_records_from_arwens_own_modules_are_redacted` | proven |
| …including in exception messages | `test_cli.py::TestPasswordRedaction::test_exception_messages_are_redacted` | proven |
| Warn, never abort, on permissions looser than `0600` | `test_config.py::TestPermissionsWarning` | proven |
| No other configuration comes from the environment | `test_cli.py::TestConfigurationSources::test_credentials_are_never_read_from_the_environment` | proven |

`cli._local_zone_keys` reads `TZ`, which is not a counterexample to the last
row: `TZ` is the operating system's own answer to "what is the local zone",
which §5.1 requires consulting. It is not arwen configuration.

## §4 Calendar selection

| Requirement | Proven by | Status |
| --- | --- | --- |
| Discover principal, then calendar-home-set | `test_dav.py::TestDiscovery`, `test_fake_server.py::TestPropfindDiscoveryFlow::test_full_discovery_chain` | proven |
| `PROPFIND` depth 1, `VEVENT`-capable collections | `test_discovery.py::TestDiscoverCalendars::test_filters_out_non_vevent_collections` | proven |
| Numbered list read from stdin | `test_discovery.py::TestSelectCalendarInteractive` (4 cases) | proven |
| Ambiguous or absent `--calendar` falls back to the prompt | `test_discovery.py::TestSelectCalendarPreselect` | proven |
| Non-TTY stdin without an unambiguous `--calendar` is a usage error | `test_discovery.py::TestSelectCalendarNonInteractive`, `test_cli.py::TestExitCodes::test_no_calendar_and_non_tty_stdin_is_a_usage_error` | proven |
| Nothing is cached or persisted between runs | — | unproven (no persistence code exists) |
| The `calendar-query` and client-side paths produce identical results | `test_dav.py::TestListResourcesEquivalence` (5 cases) | proven |

## §5 `delete before DATE`

### §5.1 Timezone resolution

| Requirement | Proven by | Status |
| --- | --- | --- |
| `--tz` accepts an IANA name; anything else is a usage error | `test_cli.py::TestExitCodes::test_an_unknown_timezone_is_a_usage_error` | proven |
| The default is the machine's local zone | `test_cli.py::TestLocalTimezoneDefault` (5 cases: `TZ`, `/etc/timezone`, the `/etc/localtime` link, an unrecognised candidate, the fixed-offset fallback) | proven |
| The resolved zone is always printed | `test_cli.py::TestDeleteBeforeExecute::test_the_resolved_timezone_is_always_reported`, `test_report.py::TestHumanReport::test_header_states_the_calendar_timezone_and_mode` | proven |
| `TZID` and trailing `Z` compare as absolute instants | `test_recurrence.py::test_tzid_dtend_and_equivalent_utc_dtend_are_the_same_instant` | proven |
| Floating date-times are interpreted in the resolved zone | `test_recurrence_classification.py::test_floating_datetime_is_resolved_in_the_run_timezone`, `test_recurrence_pruning.py::test_floating_series_is_pruned_in_the_resolved_zone` | proven |
| All-day events compare as pure dates, exclusive `DTEND` | `test_recurrence_classification.py::test_all_day_event_with_exclusive_dtend_on_boundary_is_deleted` | proven |

### §5.2 Effective end

| Requirement | Proven by | Status |
| --- | --- | --- |
| `DTEND`, else `DTSTART` + `DURATION`, else `DTSTART` (+1 day if all-day) | `test_recurrence.py` (all 8 cases) | proven |

### §5.3 Non-recurring events

| Requirement | Proven by | Status |
| --- | --- | --- |
| Ends entirely before `DATE` → delete | `test_recurrence_classification.py::test_event_ending_entirely_before_date_is_deleted` | proven |
| Straddles the boundary → skip, warn, report; never truncate | `test_recurrence_classification.py::test_event_straddling_date_is_skipped`, `test_cli.py::TestDeleteBeforeExecute::test_a_straddling_event_is_skipped_and_never_truncated` (byte-for-byte) | proven |
| Starts on or after `DATE` → untouched | `test_recurrence_classification.py::test_event_starting_on_or_after_date_is_untouched`, `::test_event_starting_exactly_on_date_is_untouched` | proven |

### §5.4 Recurring events

| Requirement | Proven by | Status |
| --- | --- | --- |
| Recurring = `RRULE`, `RDATE` or `RECURRENCE-ID` on **any** component | `test_recurrence_pruning.py::test_recurring_detection_covers_every_component`, `::test_vtimezone_rrule_does_not_make_a_resource_recurring` | proven |
| Step 1: all occurrences over → delete the resource | `::test_resource_whose_every_occurrence_is_over_is_deleted` | proven |
| Step 1: none affected → untouched | `::test_resource_with_no_affected_occurrence_is_untouched` | proven |
| Step 2: `DTSTART` shift preserves duration | `::test_dtstart_shift_preserves_the_event_duration`, `::test_duration_property_is_kept_as_is_when_dtstart_shifts` | proven |
| Step 2: **`COUNT` → `UNTIL` from the original `DTSTART`** | `::test_count_is_converted_to_until_computed_from_the_original_dtstart` | proven |
| Step 2: stale `RDATE` / `EXDATE` / overrides dropped | `::test_rdate_before_the_new_dtstart_is_dropped_and_later_ones_are_kept`, `::test_exdate_before_…`, `::test_override_before_the_boundary_is_removed_and_a_later_one_is_kept` | proven |
| Step 2: right-hand `UNTIL` left alone | `::test_explicit_until_is_left_alone_by_the_shift` | proven |
| Step 3: a straddling occurrence survives whole | `::test_straddling_occurrence_survives_whole_and_becomes_the_new_dtstart` | proven |
| Step 4: validation gate falls back to `EXDATE`-only | `::test_validation_failure_falls_back_to_exdate_only_pruning` | proven |
| Step 4: an unvalidatable resource is skipped, never written | `::test_resource_that_cannot_be_pruned_safely_is_skipped` | proven |
| Step 5: `SEQUENCE` bumped, stamps refreshed | `::test_sequence_is_bumped_and_stamps_are_refreshed` | proven |
| Step 5: all other properties preserved verbatim | `::test_unknown_and_x_properties_survive_pruning_verbatim`, `::test_folded_escaped_non_ascii_text_survives_pruning_verbatim` | proven |
| Step 6: unbounded series stay unbounded | `::test_unbounded_series_is_shifted_and_stays_unbounded` | proven |
| A single `PUT` per pruned resource | `test_cli.py::TestScheduleReplyInvariant::test_delete_before_execute_sets_the_header_on_every_mutation` (asserts the exact request set) | partial |
| The engine is pure — no mutation of its input | `::test_pruning_never_mutates_the_calendar_it_was_given` | proven |

§10 requires the pruning tests to assert against **explicitly written**
instants rather than a second call to the expander. `test_recurrence_pruning.py`
does: see `::test_pruning_keeps_exactly_the_occurrences_that_reach_the_boundary`
and the literal `DTSTART`/`DTEND` assertions throughout.

## §6 `delete duplicates`

| Requirement | Proven by | Status |
| --- | --- | --- |
| §6.1 Recurring and non-`VEVENT` resources are not examined | `test_dedup.py::test_is_examinable_excludes_recurring_and_non_vevent_resources`, `test_cli.py::TestDeleteDuplicatesExecute::test_recurring_resources_are_reported_as_not_deduplicated` | proven |
| §6.2 `SUMMARY` trimmed, collapsed, case-sensitive; missing → empty | `test_dedup.py::test_normalize_summary_*` (3 cases) | proven |
| §6.2 Start/end as absolute instants; equivalent `TZID`s match | `test_dedup.py::test_key_matches_across_tzid_and_equivalent_utc_instant` | proven |
| §6.2 Timed and all-day never match | `test_dedup.py::test_timed_and_all_day_events_never_share_a_key` | proven |
| §6.3 step 1–2 Grouping by key, then by content hash | `test_dedup.py::test_content_hash_ignores_the_excluded_properties_and_property_order` | proven |
| §6.3 step 2 The hash covers the `VEVENT`, not the `VCALENDAR` | `test_dedup.py::TestContentHashScope` (8 cases, incl. Thunderbird vs DAVx5 shapes) | proven |
| §6.3 step 2 Every `icalendar` value type hashes | `test_dedup.py::TestContentHashValueTypes` (9 cases) | proven |
| §6.3 step 3 Group, never pairwise | `test_dedup.py::test_five_identical_copies_reduce_to_exactly_one_survivor` | proven |
| §6.3 step 4 Mixed sub-groups reduce and flag for review | `test_dedup.py::test_three_identical_plus_two_divergent_needs_review`, `test_cli.py::TestDeleteDuplicatesExecute::test_three_identical_plus_two_divergent_leaves_three_survivors` | proven |
| Tie-break order, independent of server ordering | `test_dedup.py::test_tie_break_winner_is_independent_of_input_order`, `test_dav.py::TestListResourcesEquivalence::test_shuffled_server_order_does_not_change_the_result_set` | proven |
| Post-condition: exactly one survivor for *N* = 2, 5, 20 | `test_dedup.py::test_grouping_post_condition_exactly_one_survivor_for_n_2_5_20`, `test_cli.py::TestDeleteDuplicatesExecute::test_identical_copies_reduce_to_exactly_one_survivor` | proven |
| Same key, different `RRULE` → untouched | `test_dedup.py::test_same_key_different_rrule_is_never_touched` | proven |

## §7 Safety model

| Requirement | Proven by | Status |
| --- | --- | --- |
| Backup written before the first mutating request | `test_cli.py::TestBackup::test_backup_holds_the_pre_mutation_content_of_every_changed_resource` | proven |
| A failing backup aborts without touching the server | `test_cli.py::TestBackup::test_a_failing_backup_leaves_the_server_untouched` (asserts an empty mutation log) | proven |
| The file is flushed and `fsync`-ed | — | **unproven** (called at `backup.py:177-181`; asserting it needs a mock, which §10 forbids) |
| Filename is `arwen-<slug>-<UTC timestamp>.ics` | `test_backup.py::TestBackupPath` | proven |
| Dry runs write no backup | `test_cli.py::TestBackup::test_no_backup_is_written_when_there_is_nothing_to_mutate` | proven |
| Every `PUT`/`DELETE` carries `If-Match` with the scanned ETag | `test_dav.py::TestMutations::test_put_and_delete_carry_if_match_and_schedule_reply` | proven |
| `412` → skip, record a conflict, continue | `test_dav.py::TestMutations::test_put_conflict_raises_precondition_failed`, `test_cli.py::TestConflictHandling` | proven |
| Exit `0` clean | `test_cli.py::TestExitCodes::test_a_clean_run_exits_zero` | proven |
| Exit `1` skips, conflicts, review | `test_report.py::TestHasFindings`, `test_cli.py::TestConflictHandling` | proven |
| Exit `2` usage error | `test_cli.py::TestExitCodes` (5 cases) | proven |
| Exit `3` connection failure | `test_cli.py::TestExitCodes::test_an_unreachable_server_is_a_connection_failure` | proven |
| Exit `130` interrupted | `test_cli.py::TestInterrupt::test_an_interrupt_at_the_calendar_prompt_exits_130` | proven |

## §8 Output

| Requirement | Proven by | Status |
| --- | --- | --- |
| Same information and shape in dry-run and execute mode | `test_cli.py::TestReportShape::test_json_keys_are_identical_in_both_modes`, `test_report.py::TestJsonReport::test_shape_is_identical_in_both_modes` | proven |
| An explicit flag distinguishes the two | `test_report.py::TestHumanReport::test_execute_mode_says_so` | proven |
| Header: calendar, resolved timezone, mode | `test_report.py::TestHumanReport::test_header_states_the_calendar_timezone_and_mode` | proven |
| All seven counts | `test_report.py::TestCounts` | proven |
| One line per resource: UID, summary, start, action | `test_report.py::TestHumanReport::test_one_line_per_affected_resource`, `test_cli.py::TestReportShape::test_the_human_report_names_every_affected_resource` | proven |
| Each RFC 5545 flavour renders distinguishably | `test_report.py::TestFormatEventTime` | proven |
| Logging to stderr, credentials never in it | `test_cli.py::TestPasswordRedaction` | proven |

## §9 Project layout

| Requirement | Proven by | Status |
| --- | --- | --- |
| `dav.py` is the only module touching `caldav` | — | unproven (enforced by review; `pyrefly` strict stops `Any` leaking, which is the effect that matters) |
| `recurrence.py` and `dedup.py` are pure, no I/O | `test_recurrence_pruning.py::test_pruning_never_mutates_the_calendar_it_was_given`, `test_backup.py::TestRenderBackup::test_the_callers_calendar_is_never_mutated` | partial |

## §10 Tests

| Requirement | Proven by | Status |
| --- | --- | --- |
| Layer 1 unit tests over `tests/fixtures/` | `test_recurrence*.py`, `test_dedup.py`, `test_backup.py`, `test_report.py`, `test_config.py` | proven |
| Every pathological case in the §10 list | one fixture and one test each — see `tests/fixtures/` | proven |
| Layer 2 fake server: shuffled order, forced `412`, no `calendar-query`, request recording | `test_fake_server.py` (19 cases) | proven |
| Layer 3 integration tests behind `ARWEN_IT_*` | — | **absent** (§10 calls them optional and never required for a green build) |
| Property: pruning never changes a surviving occurrence | `test_recurrence_pruning.py::test_pruning_keeps_exactly_the_occurrences_that_reach_the_boundary` | proven |
| Property: one survivor per acted-upon key | `test_dedup.py::test_grouping_post_condition_exactly_one_survivor_for_n_2_5_20` | proven |
| Property: no operation increases the resource count | `test_cli.py::TestResourceCountInvariant` (both commands, dry run and execute) | proven |
| Property: a dry run issues zero mutating requests | `test_cli.py::TestDryRunIssuesNoMutations` | proven |
| Property: no request omits `Schedule-Reply: F` | `test_cli.py::TestScheduleReplyInvariant` | proven |

## §11 Documentation

| Requirement | Proven by | Status |
| --- | --- | --- |
| Docstrings on every public function | `ruff` rule set `D`, a §1.1 gate | proven |
| README covers the six documented limitations | — | unproven (prose) |

## §12 Out of scope

Nothing here is implemented, and nothing should be. There is no `backup` or
`restore` CLI surface, no recurring de-duplication, no `--from`, no fifth
runtime dependency, and no persistence between runs.

---

## Open gaps

Ordered by what a violation would cost.

1. **`fsync` (§7).** Genuinely hard to assert without a mock, which §10
   forbids. Left deliberately: the durability requirement is met by
   construction and the ordering requirement — backup before mutation — is
   proven separately.
2. **No branching on server product, version or vendor (§1).** Enforced by
   review. A grep-shaped test would catch the obvious cases and none of the
   subtle ones.
3. **`recurring-ical-events` is used for expansion only (§1).** The single
   call site at `recurrence.py:203` is the invariant; nothing pins it there.
4. **`dav.py` is the only module touching `caldav` (§9).** Likewise a single
   import site, enforced by review.
5. **Layer 3 integration tests (§10).** Absent by design. Worth building only
   when a real server is available to point them at.

The seven gaps that stood here before are closed; their rows above now name
the test that would fail. Two were rewritten during that work because the
first version passed for the wrong reason: asserting `EXIT_USAGE` from
`main` proved nothing, since nearly every bad invocation exits 2, so both
now assert against the parser directly. Each new absence-shaped test was
checked by mutation — the invariant was broken on purpose and the test was
confirmed red — rather than by observing that it passed.

### Not a §-requirement, tracked here because nothing else would

- **The multistatus response body is unbounded.** `dav._parse_multistatus`
  parses whatever the server returns, with no ceiling on its size. The `noqa`
  at `dav.py:245` waives bandit's `S314` on the grounds that this — not XML
  entity expansion — is the real exposure, and points here for it. A cap on
  the bytes read, before parsing, closes it. The same call site also sends
  every href of a collection in one `calendar-multiget` REPORT
  (`dav._multiget_bodies`), which is the same missing limit seen from the
  request side.
