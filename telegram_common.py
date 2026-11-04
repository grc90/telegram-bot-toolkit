"""Clasificacion de errores de Telegram y helpers de reintento compartidos por los bots."""

from __future__ import annotations

import telebot
from requests import exceptions as requests_exceptions


TRANSIENT_TELEGRAM_MARKERS = (
    "timed out",
    "timeout",
    "temporarily unavailable",
    "try again later",
    "bad gateway",
    "gateway timeout",
    "service unavailable",
    "too many requests",
    "retry after",
    "flood control",
    "proxy",
)


def describe_telegram_error(error):
    if isinstance(error, telebot.apihelper.ApiTelegramException):
        return f"{type(error).__name__} {error.error_code}: {error.description}"

    return type(error).__name__


def should_pause_for_telegram_error(error):
    """True for failures that are worth retrying later (network, 5xx, flood control)."""

    if isinstance(
        error,
        (
            telebot.apihelper.ApiHTTPException,
            requests_exceptions.RequestException,
            TimeoutError,
        ),
    ):
        return True

    if not isinstance(error, telebot.apihelper.ApiTelegramException):
        return False

    description = (getattr(error, "description", "") or "").lower()
    return error.error_code >= 500 or any(marker in description for marker in TRANSIENT_TELEGRAM_MARKERS)


def telegram_retry_after_seconds(error) -> int | None:
    """Extract Telegram's retry_after value from a 429 response, if present."""

    try:
        result = getattr(error, "result_json", None) or {}
        params = result.get("parameters", {}) or {}
        value = int(params.get("retry_after", 0))
        return value if value > 0 else None
    except Exception:
        return None


def rewind_media_arguments(args, kwargs):
    # El intento fallido deja el stream consumido: sin rebobinar, el reintento
    # sube 0 bytes y Telegram responde "file must be non-empty".
    for value in (*args, *kwargs.values()):
        if hasattr(value, "seek") and hasattr(value, "read"):
            try:
                value.seek(0)
            except (OSError, ValueError):
                pass
