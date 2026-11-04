"""Helpers y motor compartido para los bots de posteo."""

import math
import os
import random
import re
import threading
import time
from collections import deque
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import telebot
from telebot import types

from backup_state import (
    DEFAULT_QUEUE_RETENTION_HOURS,
    clean_old_backup_folders,
    create_backup,
    latest_backup_age_seconds,
)
from bot_logging import get_bot_logger
from config_env import validate_telegram_token
from json_storage import JsonStorageError, read_json_file, write_json_atomic
from queue_dedupe import dedupe_queue, normalize_source_url, post_signature, strip_twitter_tracking
from recent_post_guard import filter_recent_posts, record_published_post, was_posted_recently
from telegram_common import describe_telegram_error, should_pause_for_telegram_error, telegram_retry_after_seconds


_BACKUP_INTERVAL_SECONDS = 6 * 3600  # 4 times per day

# Resultado de publish_post. UNSENDABLE: Telegram rechazo este post en particular
# y reintentarlo no cambia nada; FAILED: cualquier otro fallo, incluido uno que
# podria afectar a todo el canal (el post vuelve a la queue).
PUBLISH_RESULT_SENT = "sent"
PUBLISH_RESULT_FAILED = "failed"
PUBLISH_RESULT_UNSENDABLE = "unsendable"
PUBLISH_RESULT_STOPPED = "stopped"

# Rechazos 400 que dependen del post y no del canal. Lista cerrada a proposito: un
# error desconocido (permisos, chat not found, ...) no debe vaciar la queue.
POST_SPECIFIC_REJECTION_MARKERS = (
    "wrong file identifier",
    "wrong remote file identifier",
    "wrong file_id",
    "file reference",
    "photo_invalid_dimensions",
    "image_process_failed",
    "media_empty",
    "file must be non-empty",
    "can't parse entities",
    "group send failed",
)


def _escape_markdown_url_parens(url):
    """Percent-encode literal '(' / ')' so they can't close a Markdown [label](url) link early."""

    return url.replace("(", "%28").replace(")", "%29")


def _unescape_markdown_url_parens(url):
    return url.replace("%28", "(").replace("%29", ")")


def find_latest_backup_for_queue(queue_file_path):
    try:
        queue_path = Path(queue_file_path)
        backup_root = queue_path.parent / "backups"

        if not backup_root.is_dir():
            return None

        filename = queue_path.name
        candidates = sorted(backup_root.iterdir(), key=lambda p: p.name, reverse=True)

        for backup_dir in candidates:
            if not backup_dir.is_dir():
                continue
            candidate = backup_dir / filename
            if candidate.is_file():
                return candidate
    except Exception:
        pass

    return None


def resolve_schedule_timezone(name):
    normalized_name = (name or "UTC").strip() or "UTC"

    if normalized_name.upper() == "UTC":
        return timezone.utc, "UTC"

    try:
        return ZoneInfo(normalized_name), normalized_name
    except ZoneInfoNotFoundError:
        print(
            f"Timezone '{normalized_name}' is unavailable. Falling back to UTC. "
            "Install tzdata if you want IANA time zone support on Windows."
        )
        return timezone.utc, "UTC"


def describe_hidden_queue_reasons(removed_duplicates, removed_recent, removed_malformed):
    parts = []

    if removed_duplicates:
        parts.append(f"{removed_duplicates} duplicado(s)")

    if removed_recent:
        parts.append(f"{removed_recent} publicado(s) recientemente")

    if removed_malformed:
        parts.append(f"{removed_malformed} invalido(s)")

    return ", ".join(parts) if parts else "0"


def build_queue_status_message(
    visible_count,
    stored_count,
    removed_duplicates,
    removed_recent,
    removed_malformed,
    special_mode,
    special_count,
):
    if visible_count == 0 and stored_count == 0:
        response = "La queue esta vacia. Mandame fotos o un album para cargarla."
    else:
        response = f"Hay {visible_count} post(s) en queue."

    if stored_count != visible_count:
        hidden_reasons = describe_hidden_queue_reasons(removed_duplicates, removed_recent, removed_malformed)
        response = (
            f"Hay {visible_count} post(s) visibles en queue "
            f"({stored_count} guardados en archivo; ocultos: {hidden_reasons})."
        )

    if special_mode:
        response += (
            " El modo especial sigue activo: lo nuevo se guarda aparte y se publicara al usar /done. "
            f"Hay {special_count} post(s) especial(es) cargados."
        )
    elif special_count:
        response += f" Hay {special_count} post(s) especial(es) pendientes de publicar."

    return response


def source_match_key(source_url):
    """Comparison key used by /delete to match a URL against a queued post's source.

    Looser than the dedupe signature on purpose: dedupe keeps the scheme (http:// and
    https:// are different posts to it), but an operator pasting a link back into the
    bot should not have to reproduce it byte for byte. So the scheme is dropped from
    the key, and an input without one ("danbooru.donmai.us/posts/1") is still matched.
    Host/path/query normalization stays exactly the dedupe one, so anything dedupe
    considers the same post is considered the same post here too.

    Returns None when there is nothing usable to match on.
    """

    if not source_url or not isinstance(source_url, str):
        return None

    candidate = source_url.strip()

    if not candidate:
        return None

    if not urlparse(candidate).scheme:
        candidate = f"https://{candidate.lstrip('/')}"

    normalized = normalize_source_url(candidate)

    if not normalized:
        return None

    parsed = urlparse(normalized)

    if not parsed.netloc:
        return normalized.lower()

    return urlunparse(("", parsed.netloc, parsed.path, "", parsed.query, ""))


def describe_delete_result(removed_count, remaining_size):
    if not removed_count:
        return (
            "No encontre ningun post con esa url en la cola. "
            f"Quedan {remaining_size} post(s)."
        )

    plural = "s" if removed_count != 1 else ""
    return f"Borre {removed_count} post{plural} de la cola. Quedan {remaining_size} post(s)."


def describe_enqueue_result(status, size):
    if status == "added":
        return f"Encolado. Queue: {size} post(s)."

    if status == "duplicate":
        return "Ese post ya esta en la queue."

    if status == "recent":
        return "Ese post ya se publico hace poco, no lo vuelvo a encolar."

    return None


def describe_special_enqueue_result(added, size):
    if added:
        return f"Encolado en modo especial. Especiales: {size} post(s)."

    return "Ese post ya estaba en la cola especial."


def format_seconds_as_minutes(seconds):
    if seconds <= 0:
        return "0m"

    total_seconds = int(math.ceil(seconds))
    minutes, remaining_seconds = divmod(total_seconds, 60)

    if remaining_seconds == 0:
        return f"{minutes}m"

    return f"{minutes}m {remaining_seconds}s"


def normalize_posts_per_batch_mode(value):
    if isinstance(value, str):
        normalized_value = value.strip().lower()

        if normalized_value in {"auto", "auto2"}:
            return normalized_value

    return None


def posts_per_batch_mode_hint(mode):
    """Short, user-facing explanation of what an auto/auto2 mode actually computes."""

    if mode == "auto":
        return "1 post cada 100 en cola, redondeado arriba"

    if mode == "auto2":
        return "el doble de auto"

    return None


def load_posts_per_batch_setting(settings_file, fallback):
    try:
        settings = read_json_file(settings_file, default={}, expected_type=dict)
    except JsonStorageError:
        return fallback

    persisted_value = settings.get("posts_per_batch")

    normalized_mode = normalize_posts_per_batch_mode(persisted_value)

    if normalized_mode:
        return normalized_mode

    try:
        parsed_value = int(persisted_value)
    except (TypeError, ValueError):
        return fallback

    return parsed_value if parsed_value > 0 else fallback


def collect_media_group_file_ids(items, max_items=None):
    sorted_items = sorted(items, key=lambda item: item.message_id)

    if max_items is not None:
        sorted_items = sorted_items[:max_items]

    return [item.photo[-1].file_id for item in sorted_items]


def log_cleanup_persistence_failure(logger, queue_name, error):
    logger(
        f"Could not persist cleaned {queue_name} ({type(error).__name__}). "
        "Keeping the cleaned data in memory for this run."
    )


class PostingBotApp:
    def __init__(self, config):
        self.api_token = config["api_token"]
        self.channel_id = config["channel_id"]
        self.bot_name = config["bot_name"]
        self.display_name = config.get("display_name", self.bot_name)
        timezone_name = os.environ.get(config["timezone_env"], "UTC")
        self.schedule_timezone, self.schedule_timezone_name = resolve_schedule_timezone(timezone_name)
        self.scheduled_post_hours = list(config.get("scheduled_post_hours", [0, 6, 12, 18]))
        self.posts_per_batch = config.get("posts_per_batch", 5)
        self.batch_post_delay_seconds = config.get("batch_post_delay_seconds", 2)
        self.min_post_age_seconds = config.get("min_post_age_seconds", 0)
        self.random_backlog = config.get("random_backlog", True)
        self.media_group_size = config.get("media_group_size", 10)
        self.media_group_timeout_seconds = config.get("media_group_timeout_seconds", 2.5)
        self.default_source_url = config["default_source_url"]
        self.extra_caption_links = list(config.get("extra_caption_links", []))
        self.post_queue_file = config["post_queue_file"]
        self.settings_file = config.get(
            "settings_file",
            os.path.join(os.path.dirname(self.post_queue_file), f"{self.bot_name}_settings.json"),
        )
        self.allowed_user_id = str(config["allowed_user_id"])
        self.recent_post_window_days = config.get("recent_post_window_days", 28)
        self.max_post_messages_per_minute = config.get("max_post_messages_per_minute", 20)
        self.rate_limit_window_seconds = config.get("rate_limit_window_seconds", 60)
        self.wait_poll_interval_seconds = config.get("wait_poll_interval_seconds", 0.25)
        self.api_failure_cooldown = config.get("api_failure_cooldown", 300)
        self.scheduled_post_lateness_grace_seconds = config.get("scheduled_post_lateness_grace_seconds", 300)

        validate_telegram_token(config["bot_name"] + " TELEGRAM_TOKEN", self.api_token)
        self.bot = telebot.TeleBot(self.api_token)
        self.log_lock = threading.Lock()
        self.queue_lock = threading.Lock()
        self.publish_lock = threading.Lock()
        self.manual_batch_size_lock = threading.Lock()
        self.media_group_lock = threading.Lock()
        self.rate_limit_lock = threading.Lock()
        self.api_cooldown_lock = threading.Lock()
        self.special_mode_lock = threading.Lock()
        self.special_queue_lock = threading.Lock()
        self.manual_pause_lock = threading.Lock()
        self.post_now_event = threading.Event()
        self.stop_posting_event = threading.Event()
        self.manual_batch_size = None
        self.manually_paused = False
        self.recent_send_timestamps = deque()
        self.api_cooldown_until = 0
        self.special_mode = False
        self.special_posts = []
        self.media_groups = {}
        self.media_group_special_modes = {}
        self.logger = get_bot_logger(self.bot_name)
        self.load_runtime_settings()
        self.register_handlers()

    def posts_per_batch_setting_label(self):
        normalized_mode = normalize_posts_per_batch_mode(self.posts_per_batch)
        return normalized_mode if normalized_mode else str(self.posts_per_batch)

    def resolve_posts_per_batch_for_queue_size(self, queue_size, requested_value=None):
        value = self.posts_per_batch if requested_value is None else requested_value
        normalized_mode = normalize_posts_per_batch_mode(value)

        if normalized_mode:
            auto_posts = max(1, math.ceil(queue_size / 100))
            return auto_posts * 2 if normalized_mode == "auto2" else auto_posts

        return int(value)

    def resolve_posts_per_batch(self, requested_value=None):
        queue_size = self.queue_size()
        return self.resolve_posts_per_batch_for_queue_size(queue_size, requested_value=requested_value)

    def load_runtime_settings(self):
        try:
            settings = read_json_file(self.settings_file, default={}, expected_type=dict)
        except JsonStorageError as error:
            self.log(f"Could not load settings ({type(error).__name__}). Keeping defaults.")
            return

        persisted_value = settings.get("posts_per_batch")

        if persisted_value is None:
            return

        normalized_mode = normalize_posts_per_batch_mode(persisted_value)

        if normalized_mode:
            self.posts_per_batch = normalized_mode
            return

        try:
            parsed_value = int(persisted_value)
        except (TypeError, ValueError):
            self.log("Ignoring invalid persisted posts_per_batch value.")
            return

        if parsed_value > 0:
            self.posts_per_batch = parsed_value
        else:
            self.log("Ignoring non-positive persisted posts_per_batch value.")

    def save_runtime_settings(self):
        settings = {
            "posts_per_batch": self.posts_per_batch_setting_label(),
        }
        write_json_atomic(self.settings_file, settings)

    def set_posts_per_batch(self, value):
        normalized_mode = normalize_posts_per_batch_mode(value)

        if normalized_mode:
            self.posts_per_batch = normalized_mode
        else:
            parsed_value = int(value)

            if parsed_value <= 0:
                raise ValueError("posts_per_batch must be greater than 0")

            self.posts_per_batch = parsed_value

        self.save_runtime_settings()

    def log(self, message):
        with self.log_lock:
            self.logger.info(message)

    def send_operator_alert(self, message):
        try:
            self.bot.send_message(self.allowed_user_id, message)
        except Exception as error:
            self.log(f"Could not send operator alert ({type(error).__name__}).")

    def is_allowed_user(self, message):
        return bool(getattr(message, "from_user", None)) and str(message.from_user.id) == self.allowed_user_id

    def is_special_mode(self):
        with self.special_mode_lock:
            return self.special_mode

    def set_special_mode(self, enabled):
        with self.special_mode_lock:
            self.special_mode = enabled

    def sending_is_paused(self):
        with self.api_cooldown_lock:
            return time.time() < self.api_cooldown_until

    def pause_sending(self, context, error):
        retry_after = telegram_retry_after_seconds(error)
        cooldown = (retry_after + 2) if retry_after else self.api_failure_cooldown
        with self.api_cooldown_lock:
            self.api_cooldown_until = time.time() + cooldown

        self.log(
            f"{context}: transient Telegram failure "
            f"({describe_telegram_error(error)}). "
            f"Pausing sends for {cooldown} seconds."
        )
        self.send_operator_alert(
            f"⚠️ [{self.bot_name}] Telegram esta fallando ({describe_telegram_error(error)}). "
            f"Pauso envios por {cooldown} segundos."
        )

    def call_telegram(self, context, func, *args, **kwargs):
        return self.call_telegram_detailed(context, func, *args, **kwargs)[0]

    def call_telegram_detailed(self, context, func, *args, **kwargs):
        """Like call_telegram, but returns (result, error) so callers can classify failures."""

        if self.sending_is_paused():
            return None, None

        try:
            return func(*args, **kwargs), None
        except Exception as error:
            if should_pause_for_telegram_error(error):
                self.pause_sending(context, error)
            else:
                self.log(
                    f"{context}: Telegram rejected request "
                    f"({describe_telegram_error(error)}). Sends remain active."
                )
            return None, error

    def is_unsendable_post_error(self, error):
        if not isinstance(error, telebot.apihelper.ApiTelegramException) or error.error_code != 400:
            return False

        description = (getattr(error, "description", "") or "").lower()
        return any(marker in description for marker in POST_SPECIFIC_REJECTION_MARKERS)

    def posting_is_stopped(self):
        return self.stop_posting_event.is_set()

    def clear_stop_request(self):
        self.stop_posting_event.clear()

    def request_stop_posting(self):
        self.stop_posting_event.set()

    def is_manually_paused(self):
        with self.manual_pause_lock:
            return self.manually_paused

    def pause_publishing(self):
        with self.manual_pause_lock:
            self.manually_paused = True

    def resume_publishing(self):
        with self.manual_pause_lock:
            self.manually_paused = False

    def wait_with_stop(self, total_seconds):
        deadline = time.time() + max(0, total_seconds)

        while True:
            if self.posting_is_stopped():
                return False

            remaining = deadline - time.time()

            if remaining <= 0:
                return True

            time.sleep(min(self.wait_poll_interval_seconds, remaining))

    def message_units_for_post(self, post):
        if post["type"] == "photo":
            return 1

        if post["type"] == "media_group":
            return len(post.get("file_ids", [])) + (1 if post.get("caption") else 0)

        return 1

    def wait_for_rate_limit(self, message_units):
        while True:
            if self.posting_is_stopped():
                return False

            with self.rate_limit_lock:
                now = time.time()

                while self.recent_send_timestamps and now - self.recent_send_timestamps[0] >= self.rate_limit_window_seconds:
                    self.recent_send_timestamps.popleft()

                projected_total = len(self.recent_send_timestamps) + message_units

                if projected_total <= self.max_post_messages_per_minute:
                    for _ in range(message_units):
                        self.recent_send_timestamps.append(now)
                    return True

                wait_seconds = self.rate_limit_window_seconds - (now - self.recent_send_timestamps[0])

            self.log(
                f"Rate limit guard active: waiting {wait_seconds:.1f}s before sending "
                f"{message_units} message unit(s)."
            )

            if not self.wait_with_stop(wait_seconds):
                return False

    def is_valid_queue_post(self, post):
        if not isinstance(post, dict):
            return False

        post_type = post.get("type")

        if post_type == "photo":
            return bool(post.get("file_id"))

        if post_type == "media_group":
            file_ids = post.get("file_ids")
            return isinstance(file_ids, list) and any(file_ids)

        return False

    def normalize_queue(self, queue, *, persist_recent_prune=True):
        if not isinstance(queue, list):
            return [], 0, 0, 0

        valid_queue = []
        removed_malformed = 0

        for post in queue:
            if self.is_valid_queue_post(post):
                valid_queue.append(post)
            else:
                removed_malformed += 1

        deduped_queue = dedupe_queue(valid_queue)
        filtered_queue = deduped_queue

        try:
            filtered_queue = filter_recent_posts(
                self.bot_name,
                deduped_queue,
                window_days=self.recent_post_window_days,
                persist_pruned=persist_recent_prune,
            )
        except Exception as error:
            self.log(
                f"Could not apply recent-post guard ({type(error).__name__}). "
                "Proceeding with the deduped queue."
            )

        removed_duplicates = len(valid_queue) - len(deduped_queue)
        removed_recent = len(deduped_queue) - len(filtered_queue)
        return filtered_queue, removed_duplicates, removed_recent, removed_malformed

    def load_queue(self):
        try:
            queue = read_json_file(self.post_queue_file, default=[], expected_type=list)
            filtered_queue, removed_duplicates, removed_recent, removed_malformed = self.normalize_queue(queue)

            if removed_duplicates:
                self.log(f"Removed {removed_duplicates} duplicate post(s) from queue file.")

            if removed_malformed:
                self.log(f"Removed {removed_malformed} malformed queue item(s) from queue file.")

            if removed_recent:
                self.log(
                    f"Removed {removed_recent} queued post(s) already published "
                    f"in the last {self.recent_post_window_days} days."
                )

            if removed_duplicates or removed_recent or removed_malformed:
                try:
                    self.save_queue(filtered_queue)
                except Exception as error:
                    log_cleanup_persistence_failure(self.log, "queue", error)

            return filtered_queue
        except JsonStorageError as error:
            error_type = type(error).__name__
            self.log(f"Could not load queue ({error_type}). Trying last backup.")
            backup_path = find_latest_backup_for_queue(self.post_queue_file)

            if backup_path:
                try:
                    backup_queue = read_json_file(backup_path, default=[], expected_type=list)
                    filtered_queue, _, _, _ = self.normalize_queue(backup_queue)
                    try:
                        write_json_atomic(self.post_queue_file, filtered_queue)
                    except Exception:
                        pass
                    self.log(
                        f"Restored {len(filtered_queue)} post(s) from backup {backup_path.parent.name}."
                    )
                    self.send_operator_alert(
                        f"⚠️ [{self.bot_name}] Queue corrupta ({error_type}). "
                        f"Restauré {len(filtered_queue)} posts del backup {backup_path.parent.name}."
                    )
                    return filtered_queue
                except Exception as backup_error:
                    self.log(
                        f"Could not load backup ({type(backup_error).__name__}). Starting with empty queue."
                    )
                    self.send_operator_alert(
                        f"\U0001f6a8 [{self.bot_name}] Queue corrupta ({error_type}) y backup fallido "
                        f"({type(backup_error).__name__}). Queue vacía."
                    )
                    return []

            self.log("No backup found. Starting with empty queue.")
            self.send_operator_alert(
                f"\U0001f6a8 [{self.bot_name}] Queue corrupta ({error_type}). "
                "No hay backup disponible. Queue vacía."
            )
            return []

    def load_raw_queue(self):
        try:
            return read_json_file(self.post_queue_file, default=[], expected_type=list)
        except JsonStorageError:
            return []

    def save_queue(self, queue):
        queue = dedupe_queue(queue)
        write_json_atomic(self.post_queue_file, queue)

    def queue_size(self):
        with self.queue_lock:
            return len(self.load_queue())

    def queue_stats(self):
        with self.queue_lock:
            raw_queue = self.load_raw_queue()
            stored_count = len(raw_queue)
            visible_queue, removed_duplicates, removed_recent, removed_malformed = self.normalize_queue(raw_queue)
            visible_count = len(visible_queue)

            if removed_duplicates:
                self.log(f"Removed {removed_duplicates} duplicate post(s) from queue file.")

            if removed_recent:
                self.log(
                    f"Removed {removed_recent} queued post(s) already published "
                    f"in the last {self.recent_post_window_days} days."
                )

            if removed_malformed:
                self.log(f"Removed {removed_malformed} malformed queue item(s) from queue file.")

            if removed_duplicates or removed_recent or removed_malformed:
                try:
                    self.save_queue(visible_queue)
                except Exception as error:
                    log_cleanup_persistence_failure(self.log, "queue", error)

        return visible_count, stored_count, removed_duplicates, removed_recent, removed_malformed

    def build_status_message(self):
        visible_count, stored_count, removed_duplicates, removed_recent, removed_malformed = self.queue_stats()
        next_wait_seconds, scheduled_at = self.seconds_until_next_scheduled_batch()
        resolved_posts_per_batch = self.resolve_posts_per_batch()
        special_count = self.special_queue_size()

        with self.media_group_lock:
            pending_media_groups = len(self.media_groups)

        status_lines = [
            f"Bot: {self.display_name}",
            f"Queue visible: {visible_count}",
            f"Queue guardada: {stored_count}",
            f"Ocultos: {describe_hidden_queue_reasons(removed_duplicates, removed_recent, removed_malformed)}",
            f"Modo especial: {'si' if self.is_special_mode() else 'no'}",
            f"Especiales pendientes: {special_count}",
            f"Posts por lote: {self.posts_per_batch_setting_label()} ({resolved_posts_per_batch} efectivos ahora)",
            f"Proximo lote: {scheduled_at.strftime('%Y-%m-%d %H:%M')} {self.schedule_timezone_name}",
            f"Faltan: {format_seconds_as_minutes(next_wait_seconds)}",
            f"Rate limit: {self.max_post_messages_per_minute} msg/{self.rate_limit_window_seconds}s",
            f"Media groups max: {self.media_group_size}",
            f"Pausa manual: {'SI — usa /resume para reanudar' if self.is_manually_paused() else 'no'}",
            f"Cooldown API activo: {'si' if self.sending_is_paused() else 'no'}",
            f"Stop solicitado: {'si' if self.posting_is_stopped() else 'no'}",
            f"Albums en espera de cierre: {pending_media_groups}",
        ]
        return "\n".join(status_lines)

    def enqueue_post(self, post):
        """Add a post to the main queue.

        Returns (status, size) where status is "added", "duplicate" (already
        queued) or "recent" (already published within the recent window), so
        callers can tell the operator what actually happened instead of
        staying silent.
        """

        with self.queue_lock:
            queue = self.load_queue()
            signature = post_signature(post)
            queue_signatures = {post_signature(existing_post) for existing_post in queue}

            if signature in queue_signatures:
                self.log(f"Skipped duplicate {post['type']} post already present in queue.")
                return "duplicate", len(queue)

            if was_posted_recently(self.bot_name, post, window_days=self.recent_post_window_days):
                self.log(
                    f"Skipped {post['type']} post because it was already published "
                    f"in the last {self.recent_post_window_days} days."
                )
                return "recent", len(queue)

            queue.append(post)
            self.save_queue(queue)
            size = len(queue)

        self.log(f"Queued {post['type']} post. Queue size: {size}.")
        return "added", size

    def queue_post_source_key(self, post):
        """Match key for a queued post: its explicit source_url, else the one in its caption."""

        if not isinstance(post, dict):
            return None

        source_url = post.get("source_url") or self.extract_source_url_from_caption(post.get("caption"))
        return source_match_key(source_url)

    def remove_queued_posts_by_source_url(self, source_url):
        """Drop every queued post whose source URL matches `source_url`.

        Returns (removed_count, remaining_size). removed_count is 0 when nothing
        matched (the queue file is then left untouched). Raises ValueError when the
        argument is not a usable URL, so the caller can tell "bad input" apart from
        "no match". Posts without any source URL never match.
        """

        target_key = source_match_key(source_url)

        if not target_key:
            raise ValueError(f"unusable source url: {source_url!r}")

        with self.queue_lock:
            queue = self.load_queue()
            kept_posts = [post for post in queue if self.queue_post_source_key(post) != target_key]
            removed_count = len(queue) - len(kept_posts)

            if removed_count:
                self.save_queue(kept_posts)

            return removed_count, len(kept_posts)

    def special_queue_size(self):
        with self.special_queue_lock:
            return len(self.special_posts)

    def enqueue_special_post(self, post):
        with self.special_queue_lock:
            signature = post_signature(post)
            special_signatures = {post_signature(existing_post) for existing_post in self.special_posts}

            if signature in special_signatures:
                self.log(f"Skipped duplicate {post['type']} post already present in special queue.")
                return False, len(self.special_posts)

            self.special_posts.append(post)
            size = len(self.special_posts)

        self.log(f"Queued {post['type']} post for special batch. Special size: {size}.")
        return True, size

    def drain_special_posts(self):
        with self.special_queue_lock:
            posts = list(self.special_posts)
            self.special_posts.clear()
            return posts

    def restore_special_posts(self, posts):
        if not posts:
            return

        with self.special_queue_lock:
            self.special_posts = list(posts) + self.special_posts

    def wait_for_pending_special_media_groups(self):
        deadline = time.time() + max(self.media_group_timeout_seconds, self.wait_poll_interval_seconds)

        while time.time() < deadline:
            with self.media_group_lock:
                pending_special_groups = any(self.media_group_special_modes.values())

            if not pending_special_groups:
                return

            time.sleep(min(self.wait_poll_interval_seconds, max(0.05, deadline - time.time())))

    def pop_next_post(self):
        with self.queue_lock:
            queue = self.load_queue()

            if not queue:
                return None, 0

            index = random.randrange(len(queue)) if self.random_backlog else 0
            post = queue.pop(index)
            self.save_queue(queue)
            return post, len(queue)

    def restore_post(self, post):
        with self.queue_lock:
            queue = self.load_queue()
            queue.insert(0, post)
            self.save_queue(queue)

    def build_caption(self, source_url):
        links = [("Source", source_url or self.default_source_url), *self.extra_caption_links]
        return " | ".join(
            f"[{label}]({_escape_markdown_url_parens(strip_twitter_tracking(url))})" for label, url in links if url
        )

    def extract_source_url_from_caption(self, caption):
        if not caption:
            return None

        match = re.search(r"\[Source\]\(([^)]+)\)", caption)
        return _unescape_markdown_url_parens(match.group(1)) if match else None

    def resolve_post_caption(self, post):
        source_url = post.get("source_url") or self.extract_source_url_from_caption(post.get("caption"))
        return self.build_caption(source_url)

    def build_post(self, post_type, file_data, source_url):
        # Se guarda ya limpio: el ?s=20&t=... de Twitter/X no debe quedar en la queue.
        if source_url:
            source_url = strip_twitter_tracking(source_url)

        post = {
            "type": post_type,
            "caption": self.build_caption(source_url),
            "created_at": time.time(),
        }

        if source_url:
            post["source_url"] = source_url

        post.update(file_data)
        return post

    def dry_run_publish_batch(self, max_posts=None):
        try:
            raw_queue = read_json_file(self.post_queue_file, default=[], expected_type=list)
        except JsonStorageError as error:
            self.log(f"DRY RUN: could not load queue ({type(error).__name__}). No changes made.")
            return 0

        visible_queue, removed_duplicates, removed_recent, removed_malformed = self.normalize_queue(
            raw_queue,
            persist_recent_prune=False,
        )
        target_posts = min(
            self.resolve_posts_per_batch_for_queue_size(len(visible_queue), requested_value=max_posts),
            len(visible_queue),
        )

        if self.random_backlog and target_posts:
            preview_posts = random.sample(visible_queue, target_posts)
            selection_note = "random sample"
        else:
            preview_posts = visible_queue[:target_posts]
            selection_note = "front of queue"

        self.log(
            "DRY RUN: queue has "
            f"{len(visible_queue)} visible post(s), {removed_duplicates} duplicate(s), "
            f"{removed_recent} recent-history skip(s), {removed_malformed} malformed item(s)."
        )
        self.log(f"DRY RUN: would publish {target_posts} post(s) from {selection_note}.")

        for index, post in enumerate(preview_posts, start=1):
            self.log(
                f"DRY RUN: candidate {index}/{target_posts}: "
                f"type={post.get('type')} message_units={self.message_units_for_post(post)}"
            )

        self.log("DRY RUN: no Telegram calls, queue writes, or recent-history writes were performed.")
        print(
            f"[DRY RUN] {self.bot_name}: "
            f"{len(visible_queue)} visible | {removed_duplicates} dupes | "
            f"{removed_recent} recent-skip | {removed_malformed} malformed | "
            f"would publish {target_posts}"
        )
        return target_posts

    def publish_post(self, post, record_recent=True):
        message_units = self.message_units_for_post(post)
        caption = self.resolve_post_caption(post)

        if not self.wait_for_rate_limit(message_units):
            self.log("Posting stopped before send could start.")
            return PUBLISH_RESULT_STOPPED

        if post["type"] == "photo":
            sent, error = self.call_telegram_detailed(
                "scheduled_send_photo",
                self.bot.send_photo,
                self.channel_id,
                post["file_id"],
                caption=caption,
                parse_mode="Markdown",
            )
            if sent and record_recent:
                record_published_post(self.bot_name, post, window_days=self.recent_post_window_days)
            return self.classify_publish(sent, error)

        if post["type"] == "media_group":
            media = [types.InputMediaPhoto(file_id) for file_id in post["file_ids"][:self.media_group_size]]
            sent, error = self.call_telegram_detailed(
                "scheduled_send_media_group",
                self.bot.send_media_group,
                self.channel_id,
                media,
            )

            if sent and caption:
                self.call_telegram(
                    "scheduled_send_media_group_caption",
                    self.bot.send_message,
                    self.channel_id,
                    caption,
                    parse_mode="Markdown",
                )

            if sent and record_recent:
                record_published_post(self.bot_name, post, window_days=self.recent_post_window_days)

            return self.classify_publish(sent, error)

        self.log(f"Unknown post type: {post.get('type')}")
        return PUBLISH_RESULT_UNSENDABLE

    def classify_publish(self, sent, error):
        if sent:
            return PUBLISH_RESULT_SENT

        if error is not None and self.is_unsendable_post_error(error):
            return PUBLISH_RESULT_UNSENDABLE

        return PUBLISH_RESULT_FAILED

    def report_unsendable_post(self, post):
        source_url = post.get("source_url") or self.extract_source_url_from_caption(post.get("caption"))
        self.log(f"Dropped unsendable {post.get('type')} post (source: {source_url or 'none'}).")
        self.send_operator_alert(
            f"⚠️ [{self.bot_name}] Telegram rechazo un post de la queue y lo descarte "
            f"(ver log). Source: {source_url or 'sin source'}. Si lo queres publicar, reenvialo."
        )

    def publish_batch(self, reason, max_posts=None):
        if not self.publish_lock.acquire(blocking=False):
            self.log(f"Skipping {reason}; another batch is already running.")
            return 0

        try:
            self.clear_stop_request()

            if self.is_manually_paused():
                self.log(f"Skipping {reason}; publishing is manually paused (/pause active).")
                return 0

            if self.sending_is_paused():
                return 0

            published_count = 0
            target_posts = self.resolve_posts_per_batch(max_posts)

            for _ in range(target_posts):
                if self.sending_is_paused() or self.posting_is_stopped():
                    break

                post, remaining_queue_size = self.pop_next_post()

                if not post:
                    break

                wait_seconds = self.min_post_age_seconds - (time.time() - post.get("created_at", 0))

                if wait_seconds > 0:
                    self.restore_post(post)
                    break

                if was_posted_recently(self.bot_name, post, window_days=self.recent_post_window_days):
                    self.log(
                        f"Skipping queued {post['type']} post because it was already published "
                        f"in the last {self.recent_post_window_days} days."
                    )
                    continue

                publish_result = self.publish_post(post)

                if publish_result == PUBLISH_RESULT_SENT:
                    published_count += 1
                    self.log(f"Published scheduled {post['type']} post. Queue size: {remaining_queue_size}.")
                    if not self.wait_with_stop(self.batch_post_delay_seconds):
                        break
                elif publish_result == PUBLISH_RESULT_UNSENDABLE:
                    # Ya salio de la queue en pop_next_post: no se restaura, asi no
                    # vuelve a cortar lotes cada vez que el sorteo lo elige.
                    self.report_unsendable_post(post)
                else:
                    self.restore_post(post)
                    break

            return published_count
        finally:
            self.publish_lock.release()

    def publish_special_batch(self, reason, chat_id=None):
        self.wait_for_pending_special_media_groups()
        posts = self.drain_special_posts()
        total_posts = len(posts)

        if not total_posts:
            if chat_id is not None:
                self.call_telegram(
                    "special_done_empty_ack",
                    self.bot.send_message,
                    chat_id,
                    "Modo especial terminado. No habia posts especiales pendientes.",
                )
            return 0

        with self.publish_lock:
            self.clear_stop_request()

            if self.sending_is_paused():
                self.restore_special_posts(posts)

                if chat_id is not None:
                    self.call_telegram(
                        "special_done_paused_ack",
                        self.bot.send_message,
                        chat_id,
                        (
                            "Modo especial cerrado, pero los envios siguen en cooldown. "
                            f"Quedaron {total_posts} post(s) especiales pendientes."
                        ),
                    )
                return 0

            published_count = 0
            pending_posts = []

            for index, post in enumerate(posts):
                if self.sending_is_paused() or self.posting_is_stopped():
                    pending_posts = posts[index:]
                    break

                wait_seconds = self.min_post_age_seconds - (time.time() - post.get("created_at", 0))

                if wait_seconds > 0:
                    pending_posts = posts[index:]
                    break

                publish_result = self.publish_post(post)

                if publish_result == PUBLISH_RESULT_SENT:
                    published_count += 1
                    self.log(f"Published special {post['type']} post ({published_count}/{total_posts}).")
                    if index < total_posts - 1 and not self.wait_with_stop(self.batch_post_delay_seconds):
                        pending_posts = posts[index + 1 :]
                        break
                elif publish_result == PUBLISH_RESULT_UNSENDABLE:
                    self.report_unsendable_post(post)
                else:
                    pending_posts = posts[index:]
                    break

            if pending_posts:
                self.restore_special_posts(pending_posts)

        pending_count = len(pending_posts)

        if chat_id is not None:
            if pending_count:
                self.call_telegram(
                    "special_done_partial_ack",
                    self.bot.send_message,
                    chat_id,
                    (
                        f"Modo especial terminado. Publique {published_count} de {total_posts} post(s) especiales. "
                        f"Quedaron {pending_count} pendientes."
                    ),
                )
            else:
                self.call_telegram(
                    "special_done_complete_ack",
                    self.bot.send_message,
                    chat_id,
                    f"Modo especial terminado. Publique {published_count} post(s) especiales.",
                )

        self.log(f"{reason}: published {published_count} special post(s); {pending_count} pending.")
        return published_count

    def seconds_until_next_scheduled_batch(self):
        now = datetime.now(self.schedule_timezone)

        for hour in self.scheduled_post_hours:
            scheduled_at = datetime.combine(
                now.date(),
                datetime_time(hour=hour, tzinfo=self.schedule_timezone),
            )

            if scheduled_at > now:
                return (scheduled_at - now).total_seconds(), scheduled_at

        scheduled_at = datetime.combine(
            now.date() + timedelta(days=1),
            datetime_time(hour=self.scheduled_post_hours[0], tzinfo=self.schedule_timezone),
        )
        return (scheduled_at - now).total_seconds(), scheduled_at

    def scheduled_batch_is_too_late(self, scheduled_at):
        grace_seconds = max(0, self.scheduled_post_lateness_grace_seconds)
        lateness_seconds = (datetime.now(self.schedule_timezone) - scheduled_at).total_seconds()
        return lateness_seconds > grace_seconds, lateness_seconds

    def publisher_loop(self):
        while True:
            wait_seconds, scheduled_at = self.seconds_until_next_scheduled_batch()
            self.log(
                "Next scheduled batch at "
                f"{scheduled_at.strftime('%Y-%m-%d %H:%M')} {self.schedule_timezone_name}."
            )

            if self.post_now_event.wait(wait_seconds):
                with self.manual_batch_size_lock:
                    manual_limit = self.manual_batch_size
                reason = "manual batch"
                self.post_now_event.clear()
            else:
                too_late, lateness_seconds = self.scheduled_batch_is_too_late(scheduled_at)
                if too_late:
                    self.log(
                        "Skipping scheduled batch at "
                        f"{scheduled_at.strftime('%Y-%m-%d %H:%M')} {self.schedule_timezone_name}; "
                        f"bot woke {format_seconds_as_minutes(lateness_seconds)} late."
                    )
                    continue

                reason = "scheduled batch"
                manual_limit = self.posts_per_batch

            published_count = self.publish_batch(reason, max_posts=manual_limit)
            self.log(f"{reason}: published {published_count} post(s).")

    def queue_media_group_message(self, message):
        with self.media_group_lock:
            if message.media_group_id not in self.media_groups:
                self.media_groups[message.media_group_id] = []
                self.media_group_special_modes[message.media_group_id] = self.is_special_mode()
                threading.Thread(
                    target=self.process_media_group,
                    args=(message.media_group_id,),
                    daemon=True,
                ).start()

            self.media_groups[message.media_group_id].append(message)

    def process_media_group(self, media_group_id):
        time.sleep(self.media_group_timeout_seconds)

        with self.media_group_lock:
            items = self.media_groups.pop(media_group_id, [])
            use_special_mode = self.media_group_special_modes.pop(media_group_id, False)

        file_ids = collect_media_group_file_ids(items, max_items=self.media_group_size)

        if not file_ids:
            return

        chat_id = items[0].chat.id
        source_url = items[0].caption or None
        post = self.build_post("media_group", {"file_ids": file_ids}, source_url)

        if use_special_mode:
            added, size = self.enqueue_special_post(post)
            text = describe_special_enqueue_result(added, size)
        else:
            status, size = self.enqueue_post(post)
            text = describe_enqueue_result(status, size)

        self.call_telegram("enqueue_media_group_ack", self.bot.send_message, chat_id, text)

    def register_handlers(self):
        @self.bot.message_handler(commands=["post_now"])
        def handle_post_now(message):
            if not self.is_allowed_user(message):
                return

            parts = (message.text or "").split(maxsplit=1)
            requested_posts = self.posts_per_batch
            batch_multiplier = None

            if len(parts) > 1:
                argument = parts[1].strip().lower()
                is_multiplier = argument.startswith("x")

                try:
                    parsed_value = int(argument[1:] if is_multiplier else argument)
                except ValueError:
                    self.call_telegram(
                        "post_now_invalid_ack",
                        self.bot.send_message,
                        message.chat.id,
                        "Usa /post_now, /post_now <numero> o /post_now x<tandas>.",
                    )
                    return

                if is_multiplier:
                    batch_multiplier = parsed_value
                    requested_posts = self.resolve_posts_per_batch() * parsed_value
                else:
                    requested_posts = parsed_value

                if parsed_value <= 0:
                    self.call_telegram(
                        "post_now_invalid_range_ack",
                        self.bot.send_message,
                        message.chat.id,
                        "El numero debe ser mayor que 0.",
                    )
                    return

            with self.manual_batch_size_lock:
                self.manual_batch_size = requested_posts

            if batch_multiplier is None:
                amount_text = f"hasta {requested_posts} post(s)"
            else:
                amount_text = (
                    f"hasta {requested_posts} post(s) "
                    f"({batch_multiplier} tanda(s) de {requested_posts // batch_multiplier} efectivos)"
                )

            self.clear_stop_request()
            self.post_now_event.set()
            self.call_telegram(
                "post_now_ack",
                self.bot.send_message,
                message.chat.id,
                (
                    f"Ok. Publicando {amount_text} ahora. "
                    f"Luego sigo con los horarios programados ({self.schedule_timezone_name})."
                ),
            )

        @self.bot.message_handler(commands=["stop"])
        def handle_stop(message):
            if not self.is_allowed_user(message):
                return

            self.request_stop_posting()
            self.post_now_event.clear()
            self.call_telegram(
                "stop_ack",
                self.bot.send_message,
                message.chat.id,
                "Freno activado. El posteo en curso se detendra antes del siguiente envio.",
            )

        @self.bot.message_handler(commands=["queue"])
        def handle_queue(message):
            if not self.is_allowed_user(message):
                return

            visible_count, stored_count, removed_duplicates, removed_recent, removed_malformed = self.queue_stats()
            special_count = self.special_queue_size()
            response = build_queue_status_message(
                visible_count,
                stored_count,
                removed_duplicates,
                removed_recent,
                removed_malformed,
                self.is_special_mode(),
                special_count,
            )

            self.call_telegram(
                "queue_ack",
                self.bot.send_message,
                message.chat.id,
                response,
            )

        @self.bot.message_handler(commands=["delete"])
        def handle_delete(message):
            if not self.is_allowed_user(message):
                return

            parts = (message.text or "").split(maxsplit=1)
            requested_url = parts[1].strip() if len(parts) > 1 else ""

            if not requested_url:
                self.call_telegram(
                    "delete_usage_ack",
                    self.bot.send_message,
                    message.chat.id,
                    "Usa /delete <url>. Saco de la cola el post que tenga esa url como source.",
                )
                return

            try:
                removed_count, remaining_size = self.remove_queued_posts_by_source_url(requested_url)
            except ValueError:
                self.call_telegram(
                    "delete_invalid_ack",
                    self.bot.send_message,
                    message.chat.id,
                    "No pude leer esa url. Usa /delete <url>.",
                )
                return
            except Exception as error:
                self.log(f"Could not delete queued post by url ({type(error).__name__}).")
                self.call_telegram(
                    "delete_fail_ack",
                    self.bot.send_message,
                    message.chat.id,
                    f"⚠️ No pude actualizar la cola ({type(error).__name__}).",
                )
                return

            if removed_count:
                self.log(f"Deleted {removed_count} queued post(s) by source url. Queue size: {remaining_size}.")

            self.call_telegram(
                "delete_ack",
                self.bot.send_message,
                message.chat.id,
                describe_delete_result(removed_count, remaining_size),
            )

        @self.bot.message_handler(commands=["status"])
        def handle_status(message):
            if not self.is_allowed_user(message):
                return

            self.call_telegram(
                "status_ack",
                self.bot.send_message,
                message.chat.id,
                self.build_status_message(),
            )

        @self.bot.message_handler(commands=["help"])
        def handle_help(message):
            if not self.is_allowed_user(message):
                return

            text = (
                f"Comandos disponibles — {self.display_name}\n\n"
                "Mandame una foto o un album y lo encolo.\n"
                "/post_now [n|xN] - publicar ahora (hasta n posts, o N tandas de efectivos)\n"
                "/stop - frenar el lote en curso (no afecta lo ya encolado)\n"
                "/queue - ver estado de la cola\n"
                "/delete <url> - sacar de la cola el post con esa url de source\n"
                "/status - estado completo del bot\n"
                "/special - abrir modo especial (los proximos posts se guardan aparte)\n"
                "/done - cerrar modo especial y publicar ese lote\n"
                "/pause o /resume - pausar o reanudar publicacion programada\n"
                "/backup - backup manual de los archivos de estado\n"
                "/posts_per_batch - configurar cuantos posts salen por lote\n"
                "/help - este mensaje"
            )
            self.call_telegram("help_ack", self.bot.send_message, message.chat.id, text)

        @self.bot.message_handler(commands=["special"])
        def handle_special_start(message):
            if not self.is_allowed_user(message):
                return

            self.set_special_mode(True)
            self.call_telegram(
                "special_start_ack",
                self.bot.send_message,
                message.chat.id,
                "Modo especial activado. Todo lo que envies ahora se guardara para un lote especial y se publicara al usar /done.",
            )

        @self.bot.message_handler(commands=["done"])
        def handle_special_done(message):
            if not self.is_allowed_user(message):
                return

            self.set_special_mode(False)
            special_count = self.special_queue_size()
            self.call_telegram(
                "special_done_start_ack",
                self.bot.send_message,
                message.chat.id,
                f"Modo especial cerrado. Enviando {special_count} post(s) especiales con los limites normales.",
            )
            threading.Thread(
                target=self.publish_special_batch,
                args=("special batch", message.chat.id),
                daemon=True,
            ).start()

        @self.bot.message_handler(commands=["pause"])
        def handle_pause(message):
            if not self.is_allowed_user(message):
                return

            self.pause_publishing()
            self.call_telegram(
                "pause_ack",
                self.bot.send_message,
                message.chat.id,
                "Publicacion pausada manualmente. Usa /resume para reanudar. El bot sigue activo y aceptando posts.",
            )

        @self.bot.message_handler(commands=["resume"])
        def handle_resume(message):
            if not self.is_allowed_user(message):
                return

            self.resume_publishing()
            self.call_telegram(
                "resume_ack",
                self.bot.send_message,
                message.chat.id,
                "Publicacion reanudada. El proximo lote programado se publicara normalmente.",
            )

        @self.bot.message_handler(commands=["backup"])
        def handle_backup(message):
            if not self.is_allowed_user(message):
                return

            self.call_telegram(
                "backup_started_ack",
                self.bot.send_message,
                message.chat.id,
                "Creando backup...",
            )

            try:
                backup_dir = self.run_backup()
                self.call_telegram(
                    "backup_ok_ack",
                    self.bot.send_message,
                    message.chat.id,
                    f"\U0001f4be Backup creado: {backup_dir.name}.",
                )
                self.log(f"Manual backup created at {backup_dir.name}.")
            except Exception as error:
                self.call_telegram(
                    "backup_fail_ack",
                    self.bot.send_message,
                    message.chat.id,
                    f"⚠️ Backup falló: {type(error).__name__}.",
                )
                self.log(f"Manual backup failed ({type(error).__name__}).")

        def ack_posts_per_batch_auto_mode(chat_id, mode):
            self.set_posts_per_batch(mode)
            resolved_posts = self.resolve_posts_per_batch()
            self.call_telegram(
                "posts_per_batch_set_ack",
                self.bot.send_message,
                chat_id,
                (
                    f"Listo. posts_per_batch ahora es {mode} "
                    f"({posts_per_batch_mode_hint(mode)} -> {resolved_posts} con la queue actual)."
                ),
            )

        def handle_posts_per_batch_reply(message):
            if not self.is_allowed_user(message):
                return

            raw_value = (message.text or "").strip().lower()

            if raw_value in {"auto", "auto2"}:
                ack_posts_per_batch_auto_mode(message.chat.id, raw_value)
                return

            try:
                requested_posts = int(raw_value)
            except ValueError:
                self.call_telegram(
                    "posts_per_batch_reply_invalid_ack",
                    self.bot.send_message,
                    message.chat.id,
                    "No entendi el valor. Usa /posts_per_batch y responde con un entero mayor que 0, auto o auto2.",
                )
                return

            if requested_posts <= 0:
                self.call_telegram(
                    "posts_per_batch_reply_invalid_range_ack",
                    self.bot.send_message,
                    message.chat.id,
                    "El numero debe ser mayor que 0.",
                )
                return

            self.set_posts_per_batch(requested_posts)
            self.call_telegram(
                "posts_per_batch_set_ack",
                self.bot.send_message,
                message.chat.id,
                f"Listo. posts_per_batch ahora es {requested_posts}.",
            )

        @self.bot.message_handler(commands=["posts_per_batch"])
        def handle_posts_per_batch(message):
            if not self.is_allowed_user(message):
                return

            parts = (message.text or "").split(maxsplit=1)

            if len(parts) > 1:
                self.call_telegram(
                    "posts_per_batch_invalid_ack",
                    self.bot.send_message,
                    message.chat.id,
                    "Usa /posts_per_batch y responde despues con un numero mayor que 0, auto o auto2.",
                )
                return

            sent_message = self.call_telegram(
                "posts_per_batch_prompt_ack",
                self.bot.send_message,
                message.chat.id,
                (
                    f"posts_per_batch actual: {self.posts_per_batch_setting_label()}.\n"
                    "Decime el nuevo valor que queres usar (numero entero mayor que 0, auto o auto2)."
                ),
            )

            if sent_message:
                self.bot.register_next_step_handler(sent_message, handle_posts_per_batch_reply)

        @self.bot.message_handler(commands=["posts_per_batch_auto"])
        def handle_posts_per_batch_auto(message):
            if self.is_allowed_user(message):
                ack_posts_per_batch_auto_mode(message.chat.id, "auto")

        @self.bot.message_handler(commands=["posts_per_batch_auto2"])
        def handle_posts_per_batch_auto2(message):
            if self.is_allowed_user(message):
                ack_posts_per_batch_auto_mode(message.chat.id, "auto2")

        @self.bot.message_handler(content_types=["photo"])
        def handle_photo(message):
            if not self.is_allowed_user(message):
                return

            if message.media_group_id:
                self.queue_media_group_message(message)
                return

            source_url = message.caption or None
            post = self.build_post("photo", {"file_id": message.photo[-1].file_id}, source_url)

            if self.is_special_mode():
                added, size = self.enqueue_special_post(post)
                text = describe_special_enqueue_result(added, size)
            else:
                status, size = self.enqueue_post(post)
                text = describe_enqueue_result(status, size)

            self.call_telegram("enqueue_photo_ack", self.bot.send_message, message.chat.id, text)

    def run_backup(self):
        """Run a backup and return the backup dir, raising on failure."""
        return create_backup()

    def run_backup_retention_cleanup(self):
        """Remove entire backups/state_<timestamp>/ folders older than the retention
        window (state/settings/recent-post copies included, not just queues). Safe to
        call from multiple bots concurrently; never raises for per-folder failures."""
        return clean_old_backup_folders(retention_hours=DEFAULT_QUEUE_RETENTION_HOURS, apply=True)

    def backup_was_made_recently(self):
        # Los 3 bots de posteo corren backup_loop y cada backup copia los archivos
        # de todos: el primero que llega lo hace y los demas no lo duplican.
        age = latest_backup_age_seconds()
        return age is not None and 0 <= age < _BACKUP_INTERVAL_SECONDS / 2

    def backup_loop(self):
        while True:
            time.sleep(_BACKUP_INTERVAL_SECONDS)

            if self.backup_was_made_recently():
                self.log("Automatic backup skipped: another bot made one recently.")
            else:
                try:
                    backup_dir = self.run_backup()
                    self.log(f"Automatic backup created at {backup_dir.name}.")
                    self.send_operator_alert(
                        f"\U0001f4be [{self.bot_name}] Backup automático creado: {backup_dir.name}."
                    )
                except Exception as error:
                    self.log(f"Automatic backup failed ({type(error).__name__}).")
                    self.send_operator_alert(
                        f"⚠️ [{self.bot_name}] Backup automático falló ({type(error).__name__})."
                    )

            try:
                report = self.run_backup_retention_cleanup()
                removed = len(report["to_delete"]) - len(report["failed"])
                if removed:
                    self.log(f"Backup retention cleanup: removed {removed} old folder(s).")
                if report["failed"]:
                    self.log(f"Backup retention cleanup: {len(report['failed'])} folder(s) could not be removed.")
                    self.send_operator_alert(
                        f"⚠️ [{self.bot_name}] Limpieza de backups viejos: "
                        f"{len(report['failed'])} carpeta(s) no se pudieron borrar."
                    )
            except Exception as error:
                self.log(f"Backup retention cleanup failed ({type(error).__name__}).")

    def run_bot(self):
        self.log(
            f"{self.bot_name} starting | "
            f"channel={self.channel_id} | "
            f"posts_per_batch={self.posts_per_batch_setting_label()} | "
            f"tz={self.schedule_timezone_name} | "
            f"hours={len(self.scheduled_post_hours)} slots/day | "
            f"recent_window={self.recent_post_window_days}d"
        )
        retry_delay = 5
        threading.Thread(target=self.publisher_loop, daemon=True).start()
        threading.Thread(target=self.backup_loop, daemon=True).start()

        while True:
            try:
                self.bot.polling(non_stop=False, timeout=20, long_polling_timeout=20)
                retry_delay = 5
            except Exception as error:
                self.log(f"Polling error ({type(error).__name__}). Retrying in {retry_delay} seconds.")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 300)


def create_posting_bot(**config):
    return PostingBotApp(config)
