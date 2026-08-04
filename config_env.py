"""Small local .env loader and required-environment helpers.

This keeps secrets out of source without adding a runtime dependency.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_TELEGRAM_TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{35,}\Z")


_LOADED_ENV_FILES: set[Path] = set()


def _default_env_path() -> Path:
    return Path(__file__).resolve().with_name(".env")


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()

    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()

    if not key:
        return None

    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {'"', "'"}
    ):
        value = value[1:-1]

    return key, value


def load_env_file(path: str | os.PathLike[str] | None = None, *, override: bool = False) -> None:
    """Load key/value pairs from .env if it exists.

    Existing environment variables win by default so scheduled tasks or shell
    exports can override the local file.
    """

    env_path = Path(path) if path is not None else _default_env_path()
    env_path = env_path.resolve()

    if env_path in _LOADED_ENV_FILES and not override:
        return

    _LOADED_ENV_FILES.add(env_path)

    if not env_path.exists():
        return

    with env_path.open("r", encoding="utf-8-sig") as file:
        for line in file:
            parsed = _parse_env_line(line)

            if parsed is None:
                continue

            key, value = parsed

            if override or key not in os.environ:
                os.environ[key] = value


def get_required_env(name: str) -> str:
    """Return a required environment value or raise a secret-safe error."""

    load_env_file()
    value = os.environ.get(name)

    if value is None or value == "":
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Set it in the environment or in the local .env file."
        )

    return value


def get_optional_env(name: str, default: str | None = None) -> str | None:
    load_env_file()
    return os.environ.get(name, default)


def validate_telegram_token(name: str, value: str) -> None:
    """Raise RuntimeError if value does not look like a Telegram bot token.

    Format: <bot_id>:<alphanumeric_secret>
    Example: 123456789:ABCdefGHI-jklMNOpqrSTUvwxYZ12345678
    """
    if not _TELEGRAM_TOKEN_RE.match(value):
        raise RuntimeError(
            f"Environment variable {name!r} does not look like a valid Telegram token "
            "(expected <digits>:<35+ alphanumeric chars>). "
            "Check for copy/paste errors or leading/trailing whitespace."
        )
