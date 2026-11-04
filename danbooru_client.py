"""Shared Danbooru HTTP helpers for local bot scripts."""

from __future__ import annotations

import html
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone


DANBOORU_BASE_URL = "https://danbooru.donmai.us"
TRANSIENT_HTTP_STATUS = {429, 500, 502, 503, 504}
DEFAULT_RETRY_ATTEMPTS = 2
DEFAULT_RETRY_DELAY_SECONDS = 1.0
# Un Retry-After mas largo que esto no se espera en el lugar: se corta y el
# llamador reintenta en su proximo ciclo en vez de bloquear el hilo.
MAX_RETRY_AFTER_SECONDS = 60


class DanbooruClientError(Exception):
    """Base for Danbooru client failures that are not raw HTTP errors."""


class DanbooruResponseError(DanbooruClientError):
    """The API returned malformed or unexpected data."""


def build_url(base_url, path, params):
    query = urllib.parse.urlencode(params)
    return f"{base_url}{path}?{query}"


def _retry_after_seconds(error):
    # Solo la forma en segundos; la variante con fecha HTTP cae al backoff normal.
    headers = getattr(error, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None

    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None

    return seconds if seconds > 0 else None


def _sleep_before_retry(attempt, retry_delay_seconds, retry_after_seconds=None):
    delay = retry_delay_seconds * (2 ** attempt)

    if retry_after_seconds is not None:
        delay = max(delay, retry_after_seconds)

    time.sleep(delay)


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
        retry_after_seconds = None

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

            retry_after_seconds = _retry_after_seconds(error)

            if retry_after_seconds is not None and retry_after_seconds > MAX_RETRY_AFTER_SECONDS:
                raise
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt >= retry_attempts:
                raise DanbooruClientError("Danbooru request failed after retries") from error
        except json.JSONDecodeError as error:
            raise DanbooruResponseError("Danbooru returned invalid JSON") from error

        _sleep_before_retry(attempt, retry_delay_seconds, retry_after_seconds)

    raise DanbooruClientError("Danbooru request failed unexpectedly")


PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024
# Por URL Telegram solo baja fotos de hasta 5 MB; mas grandes hay que subirlas.
TELEGRAM_PHOTO_URL_MAX_BYTES = 5 * 1024 * 1024
# Limites de sendPhoto: Telegram rechaza con PHOTO_INVALID_DIMENSIONS si la
# suma ancho+alto pasa 10000 o si la relacion de aspecto supera 20:1.
TELEGRAM_PHOTO_MAX_TOTAL_DIMENSION = 10000
TELEGRAM_PHOTO_MAX_ASPECT_RATIO = 20


def photo_media_urls(post):
    # Solo variantes de calidad: el original primero y el sample como respaldo.
    # preview_file_url queda fuera a proposito: es el thumbnail de 180x180.
    urls = []

    if not isinstance(post, Mapping):
        return urls

    for url in (post.get("file_url"), post.get("large_file_url")):
        if url and url not in urls:
            urls.append(url)

    return urls


def media_url(post):
    urls = photo_media_urls(post)
    return urls[0] if urls else None


def photo_variant_metadata(post):
    # Danbooru publica ancho/alto por variante (y file_size solo del original),
    # asi se descartan variantes imposibles antes de gastar una descarga.
    metadata = {}
    media_asset = post.get("media_asset")

    if isinstance(media_asset, dict):
        for variant in media_asset.get("variants") or []:
            if isinstance(variant, dict) and variant.get("url"):
                metadata[variant["url"]] = (variant.get("width"), variant.get("height"), None)

    file_url = post.get("file_url")

    if file_url:
        width, height, _ = metadata.get(file_url, (post.get("image_width"), post.get("image_height"), None))
        metadata[file_url] = (width, height, post.get("file_size"))

    return metadata


def photo_variant_rejection(width, height, file_size):
    # Devuelve el motivo por el que Telegram no aceptaria la variante como foto,
    # o "" si puede intentarse. Sin metadata se intenta igual.
    if file_size and file_size > TELEGRAM_PHOTO_MAX_BYTES:
        return f"{file_size} bytes exceed the {TELEGRAM_PHOTO_MAX_BYTES} byte photo limit"

    if not width or not height:
        return ""

    if width + height > TELEGRAM_PHOTO_MAX_TOTAL_DIMENSION:
        return f"{width}x{height} exceeds the {TELEGRAM_PHOTO_MAX_TOTAL_DIMENSION}px width+height limit"

    if max(width, height) > min(width, height) * TELEGRAM_PHOTO_MAX_ASPECT_RATIO:
        return f"{width}x{height} is past the {TELEGRAM_PHOTO_MAX_ASPECT_RATIO}:1 aspect ratio limit"

    return ""


def post_url(post):
    return f"{DANBOORU_BASE_URL}/posts/{post['id']}"


def display_source_url(source):
    """Source to show in a caption: a pximg.net image URL becomes its pixiv artwork page."""

    if not source:
        return ""

    source = str(source).strip()

    try:
        parsed = urllib.parse.urlparse(source)
    except ValueError:
        return source

    pixiv_match = re.search(r"/(\d+)_p\d+\.[a-z0-9]+$", parsed.path, re.IGNORECASE)

    if pixiv_match and parsed.netloc.lower().endswith("pximg.net"):
        return f"https://www.pixiv.net/artworks/{pixiv_match.group(1)}"

    return source


def build_caption(post):
    source = display_source_url(post.get("source") or "")
    return html.escape(source or post_url(post), quote=False)


def parse_post_datetime(raw_value):
    if not raw_value or not isinstance(raw_value, str):
        return None

    normalized = raw_value.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def media_size(media):
    position = media.tell()
    media.seek(0, os.SEEK_END)
    size = media.tell()
    media.seek(position)
    return size


TELEGRAM_SOURCE_HOSTS = {
    "t.me",
    "telegram.me",
    "telegram.dog",
    "telegram.org",
    "telesco.pe",
}


def is_telegram_source(source):
    """True when the post's source points at Telegram (usually https://t.me/...).

    Those posts are reuploads of what a Telegram channel already published, so
    every bot drops them instead of re-sending them.
    """

    if not source or not isinstance(source, str):
        return False

    for candidate in source.split():
        candidate = candidate.strip().strip("<>\"'")

        if not candidate:
            continue

        if "//" not in candidate:
            # Fuentes sin esquema ("t.me/canal") tambien cuentan.
            candidate = f"//{candidate}"

        try:
            host = urllib.parse.urlsplit(candidate).netloc.lower()
        except ValueError:
            continue

        host = host.rsplit("@", 1)[-1].split(":")[0]

        if any(host == item or host.endswith(f".{item}") for item in TELEGRAM_SOURCE_HOSTS):
            return True

    return False


DRIVE_SOURCE_HOSTS = {
    "drive.google.com",
    "docs.google.com",
    "drive.usercontent.google.com",
}


def is_drive_source(source):
    """True when the post's source points at Google Drive.

    Those posts link out to a Drive file instead of the actual media host, so
    every bot drops them instead of trying to fetch/repost an inaccessible link.
    """

    if not source or not isinstance(source, str):
        return False

    for candidate in source.split():
        candidate = candidate.strip().strip("<>\"'")

        if not candidate:
            continue

        if "//" not in candidate:
            candidate = f"//{candidate}"

        try:
            host = urllib.parse.urlsplit(candidate).netloc.lower()
        except ValueError:
            continue

        host = host.rsplit("@", 1)[-1].split(":")[0]

        if any(host == item or host.endswith(f".{item}") for item in DRIVE_SOURCE_HOSTS):
            return True

    return False


def validate_post(post, *, allowed_ratings=None, require_file_url=True):
    if not isinstance(post, Mapping):
        return False, "post is not an object"

    if not post.get("id"):
        return False, "missing post id"

    if is_telegram_source(post.get("source")):
        return False, "source is a Telegram link"

    if is_drive_source(post.get("source")):
        return False, "source is a Google Drive link"

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
