"""Utilidades para detectar posts equivalentes dentro de una cola."""

import json
import re
from urllib.parse import urlparse, urlunparse


SOURCE_PATTERN = re.compile(r"\[Source\]\((.*?)\)")
GENERIC_TELEGRAM_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}
# Hosts whose post identity lives in the query string rather than the path
# (e.g. dcinside gallery links are always /mgallery/board/view?id=...&no=...),
# so the query string must be kept instead of stripped like other sources.
QUERY_IDENTITY_HOSTS_SUFFIXES = (".dcinside.com",)
QUERY_IDENTITY_HOSTS = {"dcinside.com"}


def _host_keeps_query(host):
    return host in QUERY_IDENTITY_HOSTS or host.endswith(QUERY_IDENTITY_HOSTS_SUFFIXES)


def extract_source_url(caption):
    if not caption:
        return None

    match = SOURCE_PATTERN.search(caption)
    return match.group(1).strip() if match else None


def normalize_source_url(source_url):
    if not source_url:
        return None

    parsed = urlparse(source_url.strip())

    if not parsed.scheme or not parsed.netloc:
        return source_url.strip()

    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"

    if host == "twitter.com":
        host = "x.com"

    query = parsed.query if _host_keeps_query(host) else ""

    return urlunparse((parsed.scheme.lower(), host, path, "", query, ""))


def is_generic_telegram_source(source_url):
    normalized = normalize_source_url(source_url)

    if not normalized:
        return False

    parsed = urlparse(normalized)

    if parsed.netloc.lower() not in GENERIC_TELEGRAM_HOSTS:
        return False

    return bool(parsed.path.strip("/"))


def preferred_source_signature(post):
    if not isinstance(post, dict):
        return None

    explicit_source_url = normalize_source_url(post.get("source_url"))

    if explicit_source_url:
        return ("source", explicit_source_url)

    caption_source_url = normalize_source_url(extract_source_url(post.get("caption", "")))

    if caption_source_url and not is_generic_telegram_source(caption_source_url):
        return ("source", caption_source_url)

    return None


def post_signature(post):
    if not isinstance(post, dict):
        return ("invalid", type(post).__name__, repr(post))

    source_signature = preferred_source_signature(post)

    if source_signature:
        return source_signature

    post_type = post.get("type")

    if post_type == "photo" and post.get("file_id"):
        # Fallback para posts sin fuente explicita: evita mezclar imagenes distintas.
        return ("photo", post.get("file_id"))

    if post_type == "media_group" and post.get("file_ids"):
        return ("media_group", tuple(post.get("file_ids", [])))

    return ("raw", json.dumps(post, ensure_ascii=True, sort_keys=True, default=str))


def dedupe_queue(queue):
    seen = set()
    deduped = []

    for post in queue:
        signature = post_signature(post)

        if signature in seen:
            continue

        seen.add(signature)
        deduped.append(post)

    return deduped
