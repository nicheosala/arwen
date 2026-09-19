"""Parsing of ``arwen.env`` credential files, with password redaction.

Implements brief §3 end to end: a small stdlib ``KEY=VALUE`` parser (no
``dotenv`` dependency), the three required ``ARWEN_CALDAV_*`` keys, and a
permissions warning when the file is readable or writable by anyone other
than its owner. The password never appears in :class:`Credentials`'s repr,
so an accidental ``print(credentials)`` or an uncaught exception carrying it
in its arguments cannot leak it into logs (CLAUDE.md §"non-negotiable
invariants").
"""

import logging
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_log = logging.getLogger(__name__)

_URL_KEY = "ARWEN_CALDAV_URL"
_USERNAME_KEY = "ARWEN_CALDAV_USERNAME"
# S105 below: this is the *name* of the variable that carries the password,
# not a password. The value never leaves this module.
_PASSWORD_KEY = "ARWEN_CALDAV_PASSWORD"  # noqa: S105
_REQUIRED_KEYS = (_URL_KEY, _USERNAME_KEY, _PASSWORD_KEY)


class ConfigError(Exception):
    """Raised when an ``arwen.env`` file cannot be read or is malformed."""


def _strip_quotes(value: str) -> str:
    """Strip one layer of matching surrounding quotes from ``value``, if present."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines, per brief §3.

    ``#`` starts a comment, blank lines are ignored, and a value may be
    wrapped in one layer of matching single or double quotes. This is a
    small hand-written parser, not a ``dotenv`` dependency (CLAUDE.md §1.1).

    Raises:
        ConfigError: If a non-blank, non-comment line has no ``=``.
    """
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"line {line_number}: expected KEY=VALUE, got {raw_line!r}")
        key, _, value = line.partition("=")
        values[key.strip()] = _strip_quotes(value.strip())
    return values


def _warn_if_permissions_too_open(path: Path) -> None:
    """Warn to the log (never abort) if ``path`` is readable/writable beyond its owner.

    Brief §3: "Warn (do not abort) if the file's permissions are looser than
    0600." Interpreted as: any permission bit set for group or other.
    """
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        _log.warning(
            "%s has permissions %s, looser than 0600; "
            "credentials in this file are readable by other local accounts",
            path,
            oct(mode),
        )


@dataclass(frozen=True, slots=True)
class Credentials:
    """CalDAV connection credentials read from ``arwen.env`` (brief §3).

    ``password`` is deliberately excluded from the generated repr, so a
    stray ``print(credentials)``, an uncaught exception's traceback, or a
    ``--verbose`` debug log can never leak it (brief §3 / CLAUDE.md).
    """

    url: str
    username: str
    password: str

    def __repr__(self) -> str:
        """Render without the password, so it can never leak into logs or tracebacks."""
        return f"Credentials(url={self.url!r}, username={self.username!r}, password='***')"

    @classmethod
    def from_file(cls, path: Path) -> Credentials:
        """Read and parse credentials from ``path``, per brief §3.

        Warns (does not abort) if the file's permissions are looser than
        ``0600``.

        Raises:
            ConfigError: If the file cannot be read, is malformed, or is
                missing one of the required ``ARWEN_CALDAV_*`` keys.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read credentials file {path}: {exc}") from exc

        _warn_if_permissions_too_open(path)

        values = _parse_env_text(text)
        missing = [key for key in _REQUIRED_KEYS if not values.get(key)]
        if missing:
            raise ConfigError(f"{path}: missing required key(s): {', '.join(missing)}")

        return cls(
            url=values[_URL_KEY],
            username=values[_USERNAME_KEY],
            password=values[_PASSWORD_KEY],
        )
