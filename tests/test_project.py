"""Tests for the project's own invariants, rather than for its behaviour.

Brief §1 constrains what arwen may depend on as tightly as it constrains what
arwen may do to a calendar, and a dependency added by accident is exactly the
kind of change no behavioural test would notice.
"""

import importlib.metadata
import re

_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9._-]+")
"""The distribution name at the head of a PEP 508 requirement string."""

_ALLOWED_RUNTIME_DEPENDENCIES = frozenset(
    {"caldav", "icalendar", "python-dateutil", "recurring-ical-events"}
)
"""Brief §1: these four, and nothing else. Everything else is the standard library."""


def _declared_runtime_dependencies() -> set[str]:
    """Return the distribution names arwen declares as runtime requirements."""
    names: set[str] = set()
    for requirement in importlib.metadata.requires("arwen") or []:
        matched = _REQUIREMENT_NAME.match(requirement)
        assert matched is not None, f"unparseable requirement: {requirement!r}"
        names.add(matched.group(0))
    return names


def test_the_runtime_dependency_set_is_exactly_the_four_the_brief_allows() -> None:
    """Brief §1 and §12: no HTTP client, config library, CLI framework, or date parser.

    Read from the installed metadata rather than from ``pyproject.toml``, so
    what is asserted is what a user would actually receive.
    """
    assert _declared_runtime_dependencies() == _ALLOWED_RUNTIME_DEPENDENCIES
