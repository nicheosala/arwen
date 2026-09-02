"""Unit tests for brief §3: ``arwen.env`` parsing, redaction, and the permissions warning."""

import logging
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from arwen.config import ConfigError, Credentials


def _write_env(tmp_path: Path, text: str, *, mode: int = 0o600) -> Path:
    """Write ``text`` to a fresh ``arwen.env`` under ``tmp_path`` with ``mode`` permissions."""
    path = tmp_path / "arwen.env"
    path.write_text(text)
    path.chmod(mode)
    return path


class TestParsing:
    """``KEY=VALUE`` parsing: comments, blank lines, quoting."""

    def test_parses_required_keys(self, tmp_path: Path) -> None:
        """A well-formed file yields a Credentials with all three values."""
        path = _write_env(
            tmp_path,
            "ARWEN_CALDAV_URL=https://example.org\n"
            "ARWEN_CALDAV_USERNAME=user@example.org\n"
            "ARWEN_CALDAV_PASSWORD=secret\n",
        )

        credentials = Credentials.from_file(path)

        assert credentials.url == "https://example.org"
        assert credentials.username == "user@example.org"
        assert credentials.password == "secret"

    def test_ignores_comments_and_blank_lines(self, tmp_path: Path) -> None:
        """``#`` comments and blank lines are skipped, not treated as malformed."""
        path = _write_env(
            tmp_path,
            "# a comment\n"
            "\n"
            "ARWEN_CALDAV_URL=https://example.org\n"
            "   \n"
            "# ARWEN_CALDAV_USERNAME=ignored-because-commented\n"
            "ARWEN_CALDAV_USERNAME=user@example.org\n"
            "ARWEN_CALDAV_PASSWORD=secret\n",
        )

        credentials = Credentials.from_file(path)

        assert credentials.username == "user@example.org"

    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_strips_one_layer_of_surrounding_quotes(self, tmp_path: Path, quote: str) -> None:
        """A value wrapped in matching quotes has exactly that one layer stripped."""
        path = _write_env(
            tmp_path,
            "ARWEN_CALDAV_URL=https://example.org\n"
            "ARWEN_CALDAV_USERNAME=user@example.org\n"
            f"ARWEN_CALDAV_PASSWORD={quote}se cret{quote}\n",
        )

        credentials = Credentials.from_file(path)

        assert credentials.password == "se cret"

    def test_mismatched_quotes_are_kept_verbatim(self, tmp_path: Path) -> None:
        """A value with mismatched leading/trailing quote characters is left untouched."""
        path = _write_env(
            tmp_path,
            "ARWEN_CALDAV_URL=https://example.org\n"
            "ARWEN_CALDAV_USERNAME=user@example.org\n"
            "ARWEN_CALDAV_PASSWORD='mismatched\"\n",
        )

        credentials = Credentials.from_file(path)

        assert credentials.password == "'mismatched\""

    def test_missing_key_raises_config_error(self, tmp_path: Path) -> None:
        """A file missing one of the three required keys is rejected."""
        path = _write_env(
            tmp_path, "ARWEN_CALDAV_URL=https://example.org\nARWEN_CALDAV_USERNAME=user\n"
        )

        with pytest.raises(ConfigError, match="ARWEN_CALDAV_PASSWORD"):
            Credentials.from_file(path)

    def test_malformed_line_raises_config_error(self, tmp_path: Path) -> None:
        """A non-blank, non-comment line without ``=`` is rejected."""
        path = _write_env(tmp_path, "this line has no equals sign\n")

        with pytest.raises(ConfigError, match="line 1"):
            Credentials.from_file(path)

    def test_missing_file_raises_config_error(self, tmp_path: Path) -> None:
        """A nonexistent path is reported as a ConfigError, not a raw OSError."""
        with pytest.raises(ConfigError):
            Credentials.from_file(tmp_path / "does-not-exist.env")


class TestRedaction:
    """The password must never appear in a repr, per brief §3."""

    def test_repr_omits_password(self) -> None:
        """``repr()`` shows the url and username but never the password."""
        credentials = Credentials(
            url="https://example.org", username="user@example.org", password="super-secret"
        )

        rendered = repr(credentials)

        assert "super-secret" not in rendered
        assert "example.org" in rendered
        assert "***" in rendered


class TestPermissionsWarning:
    """Brief §3: warn, but never abort, on permissions looser than 0600."""

    def test_warns_on_group_readable_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A file readable by the owning group logs a warning naming the path."""
        path = _write_env(
            tmp_path,
            "ARWEN_CALDAV_URL=https://example.org\n"
            "ARWEN_CALDAV_USERNAME=user\n"
            "ARWEN_CALDAV_PASSWORD=secret\n",
            mode=0o640,
        )

        with caplog.at_level(logging.WARNING, logger="arwen.config"):
            Credentials.from_file(path)

        assert any(str(path) in record.getMessage() for record in caplog.records)

    def test_no_warning_on_owner_only_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A file readable/writable only by its owner logs nothing."""
        path = _write_env(
            tmp_path,
            "ARWEN_CALDAV_URL=https://example.org\n"
            "ARWEN_CALDAV_USERNAME=user\n"
            "ARWEN_CALDAV_PASSWORD=secret\n",
            mode=0o600,
        )

        with caplog.at_level(logging.WARNING, logger="arwen.config"):
            Credentials.from_file(path)

        assert caplog.records == []
