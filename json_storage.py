"""Helpers para leer y persistir JSON de forma conservadora."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


REPLACE_RETRY_DELAY_SECONDS = 0.25
REPLACE_RETRY_ATTEMPTS = 20


class JsonStorageError(Exception):
    """Base para errores esperados de persistencia JSON."""


class MalformedJsonError(JsonStorageError):
    """El archivo existe pero no contiene JSON valido."""


class JsonTypeError(JsonStorageError):
    """El JSON existe pero no tiene la forma esperada."""


def replace_file_with_retries(temp_path, target_path):
    temp_path = Path(temp_path).resolve()
    target_path = Path(target_path).resolve()
    last_error = None

    for attempt in range(REPLACE_RETRY_ATTEMPTS):
        try:
            os.replace(temp_path, target_path)
            return
        except PermissionError as error:
            last_error = error

            if attempt == REPLACE_RETRY_ATTEMPTS - 1:
                break

            time.sleep(REPLACE_RETRY_DELAY_SECONDS)

    if last_error is not None:
        raise last_error


def read_json_file(path, *, default=None, expected_type=None):
    """Read a JSON file, returning default only when the file is missing.

    Malformed JSON and wrong-shape JSON raise explicit exceptions so callers can
    avoid accidentally replacing a corrupt-but-recoverable state file.
    """

    path = Path(path)

    try:
        with path.open("r", encoding="utf-8-sig") as file:
            payload = json.load(file)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as error:
        raise MalformedJsonError(f"Malformed JSON in {path}") from error

    if expected_type is not None and not isinstance(payload, expected_type):
        expected_name = getattr(expected_type, "__name__", str(expected_type))
        actual_name = type(payload).__name__
        raise JsonTypeError(f"Expected {expected_name} in {path}, got {actual_name}")

    return payload


def read_json_or_default(path, default, *, expected_type=None):
    try:
        return read_json_file(path, default=default, expected_type=expected_type)
    except JsonStorageError:
        return default


def write_json_direct(path, payload):
    """Legacy direct writer kept for explicit one-off use.

    Application state should use write_json_atomic.
    """

    path = Path(path)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def write_json_atomic(path, payload):
    path = Path(path)
    parent = path.parent if str(path.parent) else Path(".")
    parent.mkdir(parents=True, exist_ok=True)
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temp_path = Path(file.name)
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        replace_file_with_retries(temp_path, path)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise
