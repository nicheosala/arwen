"""Fixtures shared by the whole suite.

Only one lives here, and it exists because :func:`arwen.cli.main` configures
process-wide logging: see :func:`arwen.cli._configure_logging`.
"""

import logging
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """Put the root logger back the way each test found it.

    :func:`arwen.cli._configure_logging` claims the root logger for the life
    of the process — deliberately, so that nothing can leave arwen's output
    or its brief §3 password redaction to a handler someone else installed.
    Inside a test process, "the life of the process" is every test that runs
    after the one that called it, pytest's own capturing handler included.
    Snapshotting the handlers and the level here keeps that ownership from
    leaking across test boundaries.
    """
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
