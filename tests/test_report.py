"""Unit tests for brief §8's report rendering.

Layer 1 of brief §10: pure functions, no server. The properties under test
are the ones brief §8 states outright — both modes carry the same
information and the same shape, distinguished by an explicit flag — plus
the exit-code condition of brief §7, which is derived from the entries and
nothing else.
"""

import json
from datetime import UTC, date, datetime

import pytest

from arwen.model import Action, AllDayDate, FloatingDateTime, Instant
from arwen.report import Report, ReportEntry, format_event_time, render, render_human, render_json


def _entry(action: Action, uid: str = "uid-1", summary: str = "Event") -> ReportEntry:
    """Build a report entry carrying one action."""
    return ReportEntry(
        href=f"/cal/{uid}.ics",
        uid=uid,
        summary=summary,
        start="2024-01-01T09:00:00+00:00",
        action=action,
    )


def _report(*actions: Action, dry_run: bool = True, examined: int = 10) -> Report:
    """Build a report over one entry per given action."""
    return Report(
        command="delete before 2025-01-01",
        calendar="Personal",
        timezone="Europe/Rome",
        dry_run=dry_run,
        examined=examined,
        entries=tuple(_entry(action, uid=f"uid-{index}") for index, action in enumerate(actions)),
    )


class TestFormatEventTime:
    """Each RFC 5545 flavour renders in a form that shows which flavour it is (brief §5.1)."""

    def test_an_instant_carries_its_offset(self) -> None:
        """An absolute instant renders with a UTC offset."""
        value = Instant(datetime(2024, 1, 1, 9, tzinfo=UTC))

        assert format_event_time(value) == "2024-01-01T09:00:00+00:00"

    def test_a_floating_datetime_carries_no_offset(self) -> None:
        """A floating date-time renders with no offset at all — that is what makes it float."""
        # DTZ001: the missing tzinfo is the point — see brief §5.1.
        value = FloatingDateTime(datetime(2024, 1, 1, 9))  # noqa: DTZ001

        assert format_event_time(value) == "2024-01-01T09:00:00"

    def test_an_all_day_date_renders_as_a_bare_date(self) -> None:
        """An all-day value never grows a time component in the report."""
        value = AllDayDate(date(2024, 1, 1))

        assert format_event_time(value) == "2024-01-01"


class TestCounts:
    """The brief §8 counts, derived from the entries alone."""

    def test_each_action_lands_in_its_own_count(self) -> None:
        """One entry per action produces one in each corresponding count."""
        report = _report(
            Action.DELETE,
            Action.MODIFY,
            Action.SKIP_STRADDLING,
            Action.NEEDS_REVIEW,
            Action.CONFLICT,
            Action.FAILED,
            Action.SKIP_NOT_EXAMINED,
        )

        counts = report.counts()

        assert counts.to_delete == 1
        assert counts.to_modify == 1
        assert counts.skipped_straddling == 1
        assert counts.needs_review == 1
        assert counts.conflicts == 1
        assert counts.failed == 1
        assert counts.skipped_not_examined == 1
        assert counts.examined == 10

    def test_both_recurrence_skips_share_one_count(self) -> None:
        """A resource skipped by §6.1 and one skipped by §5.4 are both recurrence skips."""
        report = _report(Action.SKIP_RECURRING, Action.SKIP_UNPRUNABLE)

        assert report.counts().skipped_recurring == 2

    def test_a_kept_duplicate_winner_is_not_a_finding(self) -> None:
        """Keeping the winner of a de-duplicated group is a clean outcome, not a skip."""
        report = _report(Action.DELETE, Action.KEEP)

        assert report.has_findings() is False


class TestHasFindings:
    """Brief §7's exit-code-1 condition: skips, conflicts, or items needing review."""

    @pytest.mark.parametrize(
        "action",
        [
            Action.SKIP_STRADDLING,
            Action.SKIP_UNPRUNABLE,
            Action.SKIP_RECURRING,
            Action.SKIP_NOT_EXAMINED,
            Action.NEEDS_REVIEW,
            Action.CONFLICT,
            Action.FAILED,
        ],
    )
    def test_actions_that_need_attention(self, action: Action) -> None:
        """Every attention-worthy action makes the run report findings."""
        assert _report(action).has_findings() is True

    @pytest.mark.parametrize("action", [Action.DELETE, Action.MODIFY, Action.KEEP])
    def test_actions_that_do_not(self, action: Action) -> None:
        """A run that only deleted, modified, or kept is clean."""
        assert _report(action).has_findings() is False

    def test_an_empty_report_is_clean(self) -> None:
        """Nothing to do is a clean run, not a finding."""
        assert _report().has_findings() is False


class TestHumanReport:
    """The default report of brief §8."""

    def test_header_states_the_calendar_timezone_and_mode(self) -> None:
        """Brief §5.1: the resolved zone is printed; brief §8: the mode is explicit."""
        text = render_human(_report(Action.DELETE))

        assert "Personal" in text
        assert "Europe/Rome" in text
        assert "DRY RUN" in text

    def test_execute_mode_says_so(self) -> None:
        """The same report in execute mode differs by its flag, not its shape."""
        text = render_human(_report(Action.DELETE, dry_run=False))

        assert "EXECUTE" in text
        assert "DRY RUN" not in text

    def test_one_line_per_affected_resource(self) -> None:
        """Each entry contributes its action, start, summary, and UID."""
        text = render_human(_report(Action.DELETE, Action.MODIFY))

        assert "delete" in text
        assert "modify" in text
        assert "uid-0" in text
        assert "uid-1" in text
        assert "2024-01-01T09:00:00+00:00" in text

    def test_an_empty_report_says_nothing_was_affected(self) -> None:
        """A run with no entries states that plainly rather than printing a bare header."""
        assert "No resources were affected." in render_human(_report())

    def test_a_detail_is_appended_to_its_line(self) -> None:
        """The reason a resource was skipped travels with its line."""
        report = Report(
            command="delete before 2025-01-01",
            calendar="Personal",
            timezone="UTC",
            dry_run=True,
            examined=1,
            entries=(
                ReportEntry(
                    href="/cal/a.ics",
                    uid="uid-a",
                    summary="Straddler",
                    start="2024-12-30T09:00:00+00:00",
                    action=Action.SKIP_STRADDLING,
                    detail="starts before DATE but is still in progress; not truncated",
                ),
            ),
        )

        assert "still in progress" in render_human(report)


class TestJsonReport:
    """The ``--json`` report of brief §8."""

    def test_shape_is_identical_in_both_modes(self) -> None:
        """Only the ``dry_run`` value differs between a dry run and an execute run."""
        dry = json.loads(render_json(_report(Action.DELETE)))
        executed = json.loads(render_json(_report(Action.DELETE, dry_run=False)))

        assert dry.keys() == executed.keys()
        assert dry["counts"] == executed["counts"]
        assert dry["dry_run"] is True
        assert executed["dry_run"] is False

    def test_carries_the_same_information_as_the_human_form(self) -> None:
        """Calendar, timezone, counts, and one object per affected resource."""
        payload = json.loads(render_json(_report(Action.DELETE, Action.NEEDS_REVIEW)))

        assert payload["calendar"] == "Personal"
        assert payload["timezone"] == "Europe/Rome"
        assert payload["counts"]["examined"] == 10
        assert [resource["action"] for resource in payload["resources"]] == [
            "delete",
            "needs-review",
        ]

    def test_non_ascii_summaries_stay_readable(self) -> None:
        """A summary with accents stays as written, not escaped into ASCII noise."""
        report = Report(
            command="delete duplicates",
            calendar="Persönlich",
            timezone="UTC",
            dry_run=True,
            examined=1,
            entries=(_entry(Action.DELETE, summary="Café — déjeuner"),),
        )

        text = render_json(report)

        assert "Café — déjeuner" in text
        assert "Persönlich" in text

    def test_output_is_valid_json_with_a_trailing_newline(self) -> None:
        """The report is a single JSON document, newline-terminated for shell pipelines."""
        text = render_json(_report(Action.DELETE))

        assert text.endswith("\n")
        json.loads(text)


class TestRenderSelector:
    """``--json`` picks the machine form; its absence picks the human one."""

    def test_json_flag_selects_the_json_form(self) -> None:
        """``as_json=True`` renders the same report as :func:`render_json`."""
        report = _report(Action.DELETE)

        assert render(report, as_json=True) == render_json(report)

    def test_no_json_flag_selects_the_human_form(self) -> None:
        """``as_json=False`` renders the same report as :func:`render_human`."""
        report = _report(Action.DELETE)

        assert render(report, as_json=False) == render_human(report)
