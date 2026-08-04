"""Protecciones para no republicar el mismo contenido en una ventana reciente."""

import os
import time
from contextlib import contextmanager

from json_storage import JsonStorageError, read_json_file, write_json_atomic
from queue_dedupe import post_signature


LOCK_RETRY_SECONDS = 0.05
LOCK_TIMEOUT_SECONDS = 10
DEFAULT_WINDOW_DAYS = 28


def _signature_key(post):
    import json

    return json.dumps(post_signature(post), ensure_ascii=True, separators=(",", ":"))


def _normalize_bot_name(bot_name):
    normalized = "".join(
        character if character.isalnum() or character in ("-", "_") else "_"
        for character in (bot_name or "").strip().lower()
    )
    return normalized or "default"


def _recent_posts_file(bot_name):
    normalized_bot_name = _normalize_bot_name(bot_name)
    return os.path.join(os.path.dirname(__file__), f"recent_posts_{normalized_bot_name}.json")


def _recent_posts_lock_file(bot_name):
    return f"{_recent_posts_file(bot_name)}.lock"


@contextmanager
def _file_lock(lock_path):
    deadline = time.time() + LOCK_TIMEOUT_SECONDS

    while True:
        try:
            # Usa un archivo lock sencillo para coordinar procesos sin dependencias externas.
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            # Si el proceso anterior murió sin limpiar el lock, lo reciclamos.
            try:
                lock_age_seconds = time.time() - os.path.getmtime(lock_path)
            except FileNotFoundError:
                continue

            if lock_age_seconds >= LOCK_TIMEOUT_SECONDS:
                try:
                    os.remove(lock_path)
                    continue
                except (FileNotFoundError, PermissionError):
                    pass

            if time.time() >= deadline:
                raise TimeoutError(f"Timed out waiting for lock: {lock_path}")

            time.sleep(LOCK_RETRY_SECONDS)

    try:
        yield
    finally:
        os.close(handle)
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass


def _read_entries_unlocked(posts_file):
    try:
        entries = read_json_file(posts_file, default=[], expected_type=list)
    except JsonStorageError:
        return []

    normalized_entries = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        signature = entry.get("signature")
        recorded_at = entry.get("recorded_at")

        if not signature or not isinstance(recorded_at, (int, float)):
            continue

        normalized_entries.append(
            {
                "signature": signature,
                "recorded_at": float(recorded_at),
                "bot": entry.get("bot"),
                "post_type": entry.get("post_type"),
            }
        )

    return normalized_entries


def _write_entries_unlocked(posts_file, entries):
    write_json_atomic(posts_file, entries)


def _prune_entries(entries, window_days=DEFAULT_WINDOW_DAYS, now=None):
    # Mantiene solo firmas cuya marca temporal sigue dentro de la ventana activa.
    now = time.time() if now is None else now
    cutoff = now - (window_days * 24 * 60 * 60)
    return [entry for entry in entries if entry.get("recorded_at", 0) >= cutoff]


def _load_pruned_entries(posts_file, window_days=DEFAULT_WINDOW_DAYS, now=None):
    entries = _read_entries_unlocked(posts_file)
    pruned_entries = _prune_entries(entries, window_days=window_days, now=now)
    return entries, pruned_entries


def _write_pruned_entries_if_needed(posts_file, entries, pruned_entries):
    # Evita reescrituras cuando el archivo ya esta limpio.
    if pruned_entries != entries:
        _write_entries_unlocked(posts_file, pruned_entries)


def _signature_set(entries):
    return {entry["signature"] for entry in entries}


def was_posted_recently(bot_name, post, window_days=DEFAULT_WINDOW_DAYS, persist_pruned=True):
    posts_file = _recent_posts_file(bot_name)
    lock_file = _recent_posts_lock_file(bot_name)
    signature = _signature_key(post)

    if not persist_pruned:
        _, pruned_entries = _load_pruned_entries(posts_file, window_days=window_days)
        return signature in _signature_set(pruned_entries)

    with _file_lock(lock_file):
        entries, pruned_entries = _load_pruned_entries(posts_file, window_days=window_days)

        # Cada lectura también limpia entradas vencidas para mantener el archivo pequeño.
        if persist_pruned:
            _write_pruned_entries_if_needed(posts_file, entries, pruned_entries)

        return signature in _signature_set(pruned_entries)


def filter_recent_posts(bot_name, posts, window_days=DEFAULT_WINDOW_DAYS, persist_pruned=True):
    posts_file = _recent_posts_file(bot_name)
    lock_file = _recent_posts_lock_file(bot_name)

    if not persist_pruned:
        _, pruned_entries = _load_pruned_entries(posts_file, window_days=window_days)
        recent_signatures = _signature_set(pruned_entries)
        return [post for post in posts if _signature_key(post) not in recent_signatures]

    with _file_lock(lock_file):
        entries, pruned_entries = _load_pruned_entries(posts_file, window_days=window_days)

        if persist_pruned:
            _write_pruned_entries_if_needed(posts_file, entries, pruned_entries)

        recent_signatures = _signature_set(pruned_entries)

    return [post for post in posts if _signature_key(post) not in recent_signatures]


def record_published_post(bot_name, post, window_days=DEFAULT_WINDOW_DAYS):
    posts_file = _recent_posts_file(bot_name)
    lock_file = _recent_posts_lock_file(bot_name)
    now = time.time()
    signature = _signature_key(post)

    with _file_lock(lock_file):
        _, pruned_entries = _load_pruned_entries(posts_file, window_days=window_days, now=now)

        # Reemplaza la firma si ya existía para refrescar su fecha de publicación.
        pruned_entries = [entry for entry in pruned_entries if entry["signature"] != signature]
        pruned_entries.append(
            {
                "signature": signature,
                "recorded_at": now,
                "bot": bot_name,
                "post_type": post.get("type"),
            }
        )
        _write_entries_unlocked(posts_file, pruned_entries)
