"""Shared Danbooru HTTP helpers for local bot scripts."""

from __future__ import annotations

import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping


TRANSIENT_HTTP_STATUS = {429, 500, 502, 503, 504}
DEFAULT_RETRY_ATTEMPTS = 2
DEFAULT_RETRY_DELAY_SECONDS = 1.0


class DanbooruClientError(Exception):
    """Base for Danbooru client failures that are not raw HTTP errors."""


class DanbooruResponseError(DanbooruClientError):
    """The API returned malformed or unexpected data."""


def build_url(base_url, path, params):
    query = urllib.parse.urlencode(params)
    return f"{base_url}{path}?{query}"


def _sleep_before_retry(attempt, retry_delay_seconds):
    time.sleep(retry_delay_seconds * (2 ** attempt))


def json_request(
    base_url,
    path,
    params,
    *,
    user_agent,
    timeout=30,
    retry_attempts=DEFAULT_RETRY_ATTEMPTS,
    retry_delay_seconds=DEFAULT_RETRY_DELAY_SECONDS,
    expected_type=list,
):
    """Request JSON from Danbooru with bounded retries for transient failures."""

    url = build_url(base_url, path, params)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
    )

    for attempt in range(retry_attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))

            if expected_type is not None and not isinstance(payload, expected_type):
                expected_name = getattr(expected_type, "__name__", str(expected_type))
                raise DanbooruResponseError(
                    f"Expected {expected_name} response from Danbooru, got {type(payload).__name__}"
                )

            return payload
        except urllib.error.HTTPError as error:
            if error.code not in TRANSIENT_HTTP_STATUS or attempt >= retry_attempts:
                raise
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt >= retry_attempts:
                raise DanbooruClientError("Danbooru request failed after retries") from error
        except json.JSONDecodeError as error:
            raise DanbooruResponseError("Danbooru returned invalid JSON") from error

        _sleep_before_retry(attempt, retry_delay_seconds)

    raise DanbooruClientError("Danbooru request failed unexpectedly")


def media_url(post):
    if not isinstance(post, Mapping):
        return None

    return post.get("large_file_url") or post.get("file_url") or post.get("preview_file_url")


def validate_post(post, *, allowed_ratings=None, require_file_url=True):
    if not isinstance(post, Mapping):
        return False, "post is not an object"

    if not post.get("id"):
        return False, "missing post id"

    rating = post.get("rating")

    if not rating:
        return False, "missing rating"

    if allowed_ratings is not None and rating not in allowed_ratings:
        return False, "rating is not allowed"

    if require_file_url and not media_url(post):
        return False, "missing media url"

    return True, ""


def download_media_to_memory(
    url,
    *,
    referer,
    user_agent,
    max_bytes,
    timeout=60,
    default_name="danbooru-file",
):
    if not url:
        raise ValueError("missing media URL")

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "Referer": referer,
            "User-Agent": user_agent,
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_length = response.headers.get("Content-Length")

        try:
            declared_size = int(content_length) if content_length else None
        except (TypeError, ValueError):
            declared_size = None

        if declared_size is not None and declared_size > max_bytes:
            raise ValueError("media file is too large")

        data = bytearray()

        while True:
            chunk = response.read(1024 * 1024)

            if not chunk:
                break

            data.extend(chunk)

            if len(data) > max_bytes:
                raise ValueError("media file is too large")

    media = io.BytesIO(data)
    media.name = os.path.basename(urllib.parse.urlparse(url).path) or default_name
    media.seek(0)
    return media
