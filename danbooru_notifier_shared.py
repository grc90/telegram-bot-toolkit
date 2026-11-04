"""Motor compartido de los notifiers de imageboard (API compatible con Danbooru).

Consulta la API periodicamente y envia al operador los posts nuevos que pasan
los filtros del bot. Cada notifier es un archivo de config que llama a
create_notifier_bot(); ratings, umbrales y estado son propios de cada uno.
El bot mantiene un estado local para recordar que posts ya proceso y asi evitar
reenvios entre reinicios o entre ciclos del polling de Danbooru.
"""

import argparse
import fnmatch
import html
import sys
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone

import telebot
from requests import exceptions as requests_exceptions

from backup_state import find_latest_backup_for_file
from bot_logging import get_bot_logger
from config_env import validate_telegram_token
from tag_blacklist import BLACKLISTED_TAGS, has_blacklisted_tag
from danbooru_client import (
    DANBOORU_BASE_URL,
    PHOTO_EXTENSIONS,
    TELEGRAM_PHOTO_MAX_BYTES,
    build_caption,
    download_media_to_memory,
    json_request,
    media_size,
    parse_post_datetime,
    photo_media_urls,
    photo_variant_metadata,
    photo_variant_rejection,
    post_url,
    validate_post,
)
from json_storage import JsonStorageError, read_json_file, write_json_atomic
from telegram_common import (
    describe_telegram_error,
    rewind_media_arguments,
    should_pause_for_telegram_error,
    telegram_retry_after_seconds,
)

# Maximo que acepta /posts.json segun help:api.
DANBOORU_LIMIT = 200
API_FAILURE_COOLDOWN = 300
MAX_STORED_IDS = 10000
# Ciclos seguidos con Danbooru fallando antes de avisar al operador (3 = ~30 min
# con el intervalo por defecto). Se avisa una vez y otra al recuperarse.
DANBOORU_FAILURE_ALERT_THRESHOLD = 3
# Margen hacia atras del cursor de updated_at: cubre desfasaje de reloj con Danbooru.
UPDATED_CURSOR_MARGIN_SECONDS = 15 * 60
TOP_COMMAND_LIMIT = 50
HOT_COMMAND_LIMIT = 50

TRANSIENT_SEND_ERRORS = (requests_exceptions.RequestException, TimeoutError)
SEND_RETRY_ATTEMPTS = 2
SEND_RETRY_BACKOFF_SECONDS = 3

ANIMATION_EXTENSIONS = {"gif", "mp4"}

# Resultado de un envio: entregado, descartado a proposito (no hay variante que
# Telegram acepte como foto) o fallido por un problema pasajero. Solo el ultimo
# se vuelve a intentar en el proximo ciclo.
SEND_RESULT_SENT = "sent"
SEND_RESULT_SKIPPED = "skipped"
SEND_RESULT_FAILED = "failed"


def default_state():
    # Estado minimo para arrancar incluso si el JSON no existe o esta corrupto.
    return {
        "bootstrapped": False,
        "filter_signature": "",
        "handled_post_ids": [],
        "subscriber_chat_ids": [],
        "last_check_at": None,
        "updated_cursor": None,
    }


class DanbooruNotifierApp:
    def __init__(self, config):
        self.api_token = config["api_token"]
        self.bot_name = config["bot_name"]
        self.display_name = config.get("display_name", self.bot_name)
        self.state_file = config["state_file"]
        self.allowed_user_id = str(config["allowed_user_id"])
        self.allowed_ratings = list(config["allowed_ratings"])
        self.score_threshold = config["score_threshold"]
        # Solo posts creados hace <= N dias: order:updated tambien trae posts viejos
        # re-taggeados, y si su id ya salio de handled_post_ids (tope MAX_STORED_IDS)
        # se reenviarian. N tiene que quedar holgado dentro de esa ventana de ids.
        self.max_post_age_days = config["max_post_age_days"]
        self.danbooru_pages = config["danbooru_pages"]
        self.poll_interval_seconds = config["poll_interval_seconds"]
        self.max_download_bytes = config["max_download_bytes"]
        self.top_score_threshold = config["top_score_threshold"]
        self.hot_score_threshold = config["hot_score_threshold"]
        self.score_band_min, self.score_band_max = config["score_band"]

        self.rating_filter = f"rating:{','.join(self.allowed_ratings)}"
        self.tag_query = (
            f"{self.rating_filter} score:>={self.score_threshold} age:<={self.max_post_age_days}d order:updated"
        )
        self.hot_tag_query = f"order:rank {self.rating_filter} score:>={self.hot_score_threshold} age:<=24h"
        self.user_agent = f"{self.bot_name}/1.0 (personal Telegram notifier)"

        validate_telegram_token(f"{self.bot_name.upper()}_TELEGRAM_TOKEN", self.api_token)
        self.bot = telebot.TeleBot(self.api_token, parse_mode="HTML")
        self.logger = get_bot_logger(self.bot_name)

        self.state_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.api_cooldown_until = 0
        self.api_cooldown_lock = threading.Lock()
        self.check_now_event = threading.Event()
        self.consecutive_danbooru_failures = 0
        self.danbooru_failure_alerted = False
        self.register_handlers()

    # ------------------------------------------------------------------
    # Logging, alerts and state
    # ------------------------------------------------------------------

    def log(self, message):
        with self.log_lock:
            self.logger.info(message)

    def send_operator_alert(self, message):
        try:
            self.bot.send_message(self.allowed_user_id, message)
        except Exception as error:
            self.log(f"Could not send operator alert ({type(error).__name__}).")

    def filter_signature(self):
        blacklist = ",".join(sorted(BLACKLISTED_TAGS))
        return f"{self.tag_query} blacklist:{blacklist}"

    def load_state_unlocked(self):
        try:
            state = read_json_file(self.state_file, default=default_state(), expected_type=dict)
        except JsonStorageError as e:
            error_type = type(e).__name__
            self.log(f"Could not load state ({error_type}). Trying last backup.")
            backup_path = find_latest_backup_for_file(self.state_file)

            if backup_path:
                try:
                    state = read_json_file(backup_path, default=default_state(), expected_type=dict)
                    write_json_atomic(self.state_file, state)
                    self.log(f"Restored state from backup {backup_path.parent.name}.")
                    self.send_operator_alert(
                        f"⚠️ [{self.bot_name}] Estado corrupto ({error_type}). "
                        f"Restaure el estado desde el backup {backup_path.parent.name}."
                    )
                except Exception as backup_error:
                    self.log(f"Could not load backup ({type(backup_error).__name__}). Starting fresh.")
                    self.send_operator_alert(
                        f"\U0001f6a8 [{self.bot_name}] Estado corrupto ({error_type}) y backup fallido "
                        f"({type(backup_error).__name__}). Reinicie el estado: perdiste suscriptores e historial."
                    )
                    state = default_state()
            else:
                self.log("No backup found. Starting fresh.")
                self.send_operator_alert(
                    f"\U0001f6a8 [{self.bot_name}] Estado corrupto ({error_type}). No hay backup disponible. "
                    "Reinicie el estado: perdiste suscriptores e historial."
                )
                state = default_state()

        fresh = default_state()
        fresh.update(state if isinstance(state, dict) else {})
        # Normaliza el estado cargado para evitar duplicados y valores inesperados.
        fresh["handled_post_ids"] = list(dict.fromkeys(str(item) for item in fresh["handled_post_ids"]))
        fresh["subscriber_chat_ids"] = [
            chat_id
            for chat_id in list(dict.fromkeys(str(item) for item in fresh["subscriber_chat_ids"]))
            if chat_id == self.allowed_user_id
        ]
        return fresh

    def save_state_unlocked(self, state):
        # Limita el historial para que el archivo no crezca indefinidamente.
        state["handled_post_ids"] = state["handled_post_ids"][-MAX_STORED_IDS:]
        write_json_atomic(self.state_file, state)

    def get_state(self):
        with self.state_lock:
            return self.load_state_unlocked()

    def update_state(self, mutator):
        # Todas las escrituras del estado pasan por el mismo lock.
        with self.state_lock:
            state = self.load_state_unlocked()
            result = mutator(state)
            self.save_state_unlocked(state)
            return result

    def is_allowed_user(self, message):
        return bool(getattr(message, "from_user", None)) and str(message.from_user.id) == self.allowed_user_id

    # ------------------------------------------------------------------
    # Telegram wrappers
    # ------------------------------------------------------------------

    def sending_is_paused(self):
        with self.api_cooldown_lock:
            return time.time() < self.api_cooldown_until

    def pause_sending(self, context, error):
        retry_after = telegram_retry_after_seconds(error)
        cooldown = (retry_after + 2) if retry_after else API_FAILURE_COOLDOWN

        with self.api_cooldown_lock:
            self.api_cooldown_until = time.time() + cooldown

        self.log(
            f"{context}: transient Telegram failure "
            f"({describe_telegram_error(error)}). Pausing sends for {cooldown} seconds."
        )

    def normalize_telegram_call(self, func, args, kwargs):
        if getattr(func, "__name__", None) != "send_message" or getattr(func, "__self__", None) is not self.bot:
            return args, kwargs

        if len(args) < 2 or "parse_mode" in kwargs or not isinstance(args[1], str):
            return args, kwargs

        normalized_args = list(args)
        normalized_args[1] = html.escape(normalized_args[1], quote=False)
        return tuple(normalized_args), kwargs

    def call_telegram(self, context, func, *args, **kwargs):
        # Corta envios temporalmente cuando Telegram empieza a fallar en cascada.
        if self.sending_is_paused():
            return None

        args, kwargs = self.normalize_telegram_call(func, args, kwargs)

        try:
            return func(*args, **kwargs)
        except Exception as e:
            if should_pause_for_telegram_error(e):
                self.pause_sending(context, e)
            else:
                self.log(f"{context} failed ({describe_telegram_error(e)}).")
            return None

    def try_telegram_detailed(self, context, func, *args, **kwargs):
        # Igual que try_telegram, pero expone el error para distinguir un fallo
        # pasajero (reintentar en el proximo ciclo) de un rechazo definitivo.
        for attempt in range(SEND_RETRY_ATTEMPTS + 1):
            if attempt:
                rewind_media_arguments(args, kwargs)

            try:
                return func(*args, **kwargs), None
            except requests_exceptions.ReadTimeout as e:
                # La peticion ya salio: Telegram pudo recibir y publicar la foto y
                # solo perderse la respuesta. Reintentar la subida la duplicaria.
                self.log(f"{context} failed, response lost; not retrying in place ({type(e).__name__}).")
                return None, e
            except TRANSIENT_SEND_ERRORS as e:
                if attempt < SEND_RETRY_ATTEMPTS:
                    time.sleep(SEND_RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                self.log(f"{context} failed after {attempt + 1} attempts ({type(e).__name__}: {e}).")
                return None, e
            except Exception as e:
                self.log(f"{context} failed ({type(e).__name__}: {e}).")
                return None, e

        return None, None

    def try_telegram(self, context, func, *args, **kwargs):
        result, _error = self.try_telegram_detailed(context, func, *args, **kwargs)
        return result

    # ------------------------------------------------------------------
    # Danbooru fetch and filters
    # ------------------------------------------------------------------

    def danbooru_request(self, path, params):
        return json_request(
            DANBOORU_BASE_URL,
            path,
            params,
            user_agent=self.user_agent,
            expected_type=list,
        )

    def post_is_sendable(self, post):
        is_valid, reason = validate_post(post, allowed_ratings=set(self.allowed_ratings))

        if not is_valid:
            post_id = post.get("id") if isinstance(post, dict) else "unknown"
            self.log(f"Skipping Danbooru post {post_id}: {reason}.")
            return False

        return True

    def post_score(self, post):
        try:
            return int(post.get("score") or 0)
        except (TypeError, ValueError):
            self.log(f"Skipping Danbooru post {post.get('id')}: invalid score.")
            return None

    def download_media(self, url):
        return download_media_to_memory(
            url,
            referer=DANBOORU_BASE_URL,
            user_agent=self.user_agent,
            max_bytes=self.max_download_bytes,
        )

    def is_within_max_post_age(self, post, now=None):
        # Sin created_at legible no se descarta: la query remota ya filtra por age.
        created_at = parse_post_datetime(post.get("created_at"))

        if created_at is None:
            return True

        now = datetime.now(timezone.utc) if now is None else now
        return (now - created_at).total_seconds() <= self.max_post_age_days * 24 * 60 * 60

    def fetch_posts_by_query(self, tag_query, pages=None, per_page_limit=DANBOORU_LIMIT, updated_since=None):
        # Generador: si quien itera corta antes (p. ej. /top al llegar a su limite),
        # las paginas que faltan no se piden. Con updated_since (solo para queries
        # order:updated) deja de paginar en cuanto una pagina llega a posts
        # actualizados antes de ese momento: las siguientes son todas mas viejas.
        pages = self.danbooru_pages if pages is None else pages

        for page in range(1, pages + 1):
            page_posts = self.danbooru_request(
                "/posts.json",
                {
                    "tags": tag_query,
                    "limit": per_page_limit,
                    "page": page,
                },
            )

            if not page_posts:
                return

            yield from page_posts

            if updated_since is not None:
                page_updated_at = [
                    parsed
                    for parsed in (
                        parse_post_datetime(post.get("updated_at")) for post in page_posts if isinstance(post, dict)
                    )
                    if parsed is not None
                ]

                if page_updated_at and min(page_updated_at) < updated_since:
                    return

    def fetch_matching_posts(self, updated_since=None):
        filtered = []
        seen_ids = set()

        for post in self.fetch_posts_by_query(self.tag_query, updated_since=updated_since):
            if not self.post_is_sendable(post):
                continue

            post_id = str(post.get("id", ""))

            if not post_id or post_id in seen_ids:
                continue

            seen_ids.add(post_id)

            score = self.post_score(post)
            if score is None:
                continue

            # Revalida localmente rating, score, blacklist y antiguedad aunque ya
            # vengan en la query.
            if (
                post.get("rating") in self.allowed_ratings
                and score >= self.score_threshold
                and not has_blacklisted_tag(post)
                and self.is_within_max_post_age(post)
            ):
                filtered.append(post)

        return filtered

    def fetch_recent_score_band_posts(self, min_score=None, max_score=None, hours=6):
        min_score = self.score_band_min if min_score is None else min_score
        max_score = self.score_band_max if max_score is None else max_score
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - (hours * 60 * 60)
        tag_query = f"{self.rating_filter} score:>={min_score} score:<={max_score} order:updated"
        filtered = []
        seen_ids = set()

        for post in self.fetch_posts_by_query(tag_query, updated_since=datetime.fromtimestamp(cutoff, timezone.utc)):
            if not self.post_is_sendable(post):
                continue

            post_id = str(post.get("id", ""))

            if not post_id or post_id in seen_ids or has_blacklisted_tag(post):
                continue

            seen_ids.add(post_id)

            if post.get("rating") not in self.allowed_ratings:
                continue

            score = self.post_score(post)

            if score is None:
                continue

            if score < min_score or score > max_score:
                continue

            created_at = parse_post_datetime(post.get("created_at"))
            updated_at = parse_post_datetime(post.get("updated_at"))
            post_timestamps = [item.timestamp() for item in (created_at, updated_at) if item is not None]

            if not post_timestamps:
                continue

            freshest_at = max(post_timestamps)

            if freshest_at < cutoff:
                continue

            filtered.append(post)

        filtered.sort(
            key=lambda post: max([
                item.timestamp()
                for item in (
                    parse_post_datetime(post.get("created_at")),
                    parse_post_datetime(post.get("updated_at")),
                )
                if item is not None
            ]),
            reverse=True,
        )
        return filtered

    def fetch_top_score_posts(self, min_score, limit):
        tag_query = f"{self.rating_filter} score:>={min_score} order:updated"
        filtered = []
        seen_ids = set()

        for post in self.fetch_posts_by_query(tag_query):
            if not self.post_is_sendable(post):
                continue

            post_id = str(post.get("id", ""))

            if not post_id or post_id in seen_ids or has_blacklisted_tag(post):
                continue

            seen_ids.add(post_id)

            if post.get("rating") not in self.allowed_ratings:
                continue

            score = self.post_score(post)

            if score is None or score < min_score:
                continue

            extension = (post.get("file_ext") or "").lower()

            if extension not in PHOTO_EXTENSIONS:
                continue

            filtered.append(post)

            if len(filtered) >= limit:
                break

        return filtered

    def fetch_hot_posts(self, limit):
        filtered = []
        seen_ids = set()

        for post in self.fetch_posts_by_query(self.hot_tag_query, pages=1, per_page_limit=limit):
            if not self.post_is_sendable(post):
                continue

            post_id = str(post.get("id", ""))

            if not post_id or post_id in seen_ids or has_blacklisted_tag(post):
                continue

            seen_ids.add(post_id)

            if post.get("rating") not in self.allowed_ratings:
                continue

            score = self.post_score(post)

            if score is None or score < self.hot_score_threshold:
                continue

            extension = (post.get("file_ext") or "").lower()

            if extension not in PHOTO_EXTENSIONS:
                continue

            filtered.append(post)

            if len(filtered) >= limit:
                break

        return filtered

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    def send_post_as_photo(self, chat_id, post, caption):
        # Recorre solo variantes de calidad y sube como foto. Nunca manda documentos.
        # Mientras alguna variante se pueda entregar, la caption queda intacta: el
        # link a Danbooru solo se manda si el post no se puede enviar de ninguna
        # forma. Un fallo pasajero no avisa nada porque se reintenta solo.
        had_transient_failure = False
        last_rejection = ""
        variant_metadata = photo_variant_metadata(post)

        for image_url in photo_media_urls(post):
            width, height, file_size = variant_metadata.get(image_url, (None, None, None))
            rejection = photo_variant_rejection(width, height, file_size)

            if rejection:
                last_rejection = rejection
                self.log(f"Post {post.get('id')}: skipping {image_url} ({rejection}).")
                continue

            try:
                downloaded_media = self.download_media(image_url)
            except Exception as e:
                self.log(f"download_media failed for post {post.get('id')} ({type(e).__name__}).")
                had_transient_failure = True
                continue

            downloaded_size = media_size(downloaded_media)

            if downloaded_size == 0:
                self.log(f"download_media returned an empty file for post {post.get('id')} ({image_url}).")
                had_transient_failure = True
                continue

            if downloaded_size > TELEGRAM_PHOTO_MAX_BYTES:
                last_rejection = f"{downloaded_size} bytes exceed the {TELEGRAM_PHOTO_MAX_BYTES} byte photo limit"
                self.log(
                    f"Post {post.get('id')}: {image_url} exceeds the Telegram photo limit; trying a smaller variant."
                )
                continue

            sent, error = self.try_telegram_detailed(
                "upload_photo", self.bot.send_photo, chat_id, downloaded_media, caption=caption
            )

            if sent:
                return SEND_RESULT_SENT

            if error is not None and should_pause_for_telegram_error(error):
                # La subida pudo llegar a Telegram aunque la respuesta se perdio
                # (ReadTimeout, 502): probar otra variante duplicaria la foto en
                # el chat. Se corta aqui y se reintenta el post en el proximo ciclo.
                self.log(f"Post {post.get('id')}: photo upload outcome unknown; not trying other variants.")
                return SEND_RESULT_FAILED

            if error is not None:
                last_rejection = describe_telegram_error(error)

        if had_transient_failure:
            self.log(f"Post {post.get('id')}: photo upload failed for now; will retry on the next check.")
            return SEND_RESULT_FAILED

        self.log(f"Post {post.get('id')}: no photo-quality variant fits Telegram's limits; skipping.")
        detail = f" ({last_rejection})" if last_rejection else ""
        self.call_telegram(
            "photo_limits_notice",
            self.bot.send_message,
            chat_id,
            f"Telegram no acepta este post como foto{detail}:\n{post_url(post)}",
        )
        return SEND_RESULT_SKIPPED

    def send_post_to_chat(self, chat_id, post):
        urls = photo_media_urls(post)
        url = urls[0] if urls else None
        caption = build_caption(post)

        if not url:
            self.log(f"Skipping post {post.get('id')}: no media URL.")
            return SEND_RESULT_SKIPPED

        extension = (post.get("file_ext") or url.rsplit(".", 1)[-1]).lower()

        if extension in PHOTO_EXTENSIONS:
            return self.send_post_as_photo(chat_id, post, caption)

        if extension not in ANIMATION_EXTENSIONS:
            self.log(f"Skipping post {post.get('id')}: .{extension} would need a document upload.")
            return SEND_RESULT_SKIPPED

        # gif/mp4 se envian como animacion o video (nunca como documento): primero
        # por URL y, si Telegram no la acepta, subiendo el archivo descargado.
        by_url_sender = self.bot.send_animation if extension == "gif" else self.bot.send_video

        sent, error = self.try_telegram_detailed(f"send_{extension}", by_url_sender, chat_id, url, caption=caption)

        if sent:
            return SEND_RESULT_SENT

        if error is not None and should_pause_for_telegram_error(error):
            # Igual que con las fotos: sin un rechazo definitivo el envio por URL
            # pudo llegar, y subir el archivo lo duplicaria. Reintento el proximo ciclo.
            return SEND_RESULT_FAILED

        try:
            downloaded_media = self.download_media(url)
        except Exception as e:
            self.log(f"download_media failed for post {post.get('id')} ({type(e).__name__}).")
            return SEND_RESULT_FAILED

        if media_size(downloaded_media) == 0:
            self.log(f"download_media returned an empty file for post {post.get('id')} ({url}).")
            return SEND_RESULT_FAILED

        uploaded, error = self.try_telegram_detailed(
            f"upload_{extension}", by_url_sender, chat_id, downloaded_media, caption=caption
        )

        if uploaded:
            return SEND_RESULT_SENT

        if error is not None and should_pause_for_telegram_error(error):
            return SEND_RESULT_FAILED

        self.log(f"Skipping post {post.get('id')}: media could not be sent without using a document.")
        return SEND_RESULT_SKIPPED

    def send_post_to_subscribers(self, post):
        state = self.get_state()
        chat_ids = state["subscriber_chat_ids"]

        if not chat_ids:
            self.log(f"Post {post['id']} matched, but there are no subscribers yet.")
            return SEND_RESULT_FAILED

        results = []

        # Pequena pausa entre chats para no apilar requests seguidas a Telegram.
        for chat_id in chat_ids:
            results.append(self.send_post_to_chat(chat_id, post))
            time.sleep(1)

        if SEND_RESULT_SENT in results:
            return SEND_RESULT_SENT

        return SEND_RESULT_FAILED if SEND_RESULT_FAILED in results else SEND_RESULT_SKIPPED

    def send_posts_to_chat(self, chat_id, posts):
        sent_count = 0

        for post in posts:
            if self.send_post_to_chat(chat_id, post) == SEND_RESULT_SENT:
                sent_count += 1
                time.sleep(1)

        return sent_count

    def send_latest_matching_post(self, chat_id):
        try:
            posts = self.fetch_matching_posts()
        except Exception as e:
            self.log(f"manual test: Danbooru check failed ({type(e).__name__}).")
            return False

        if not posts:
            self.call_telegram(
                "test_no_posts",
                self.bot.send_message,
                chat_id,
                f"No encontre posts con {self.rating_filter} y score >= {self.score_threshold}.",
            )
            return False

        return self.send_post_to_chat(chat_id, posts[0]) == SEND_RESULT_SENT

    def send_recent_score_band_posts(self, chat_id, min_score=None, max_score=None, hours=6):
        min_score = self.score_band_min if min_score is None else min_score
        max_score = self.score_band_max if max_score is None else max_score

        try:
            posts = self.fetch_recent_score_band_posts(min_score=min_score, max_score=max_score, hours=hours)
        except Exception as e:
            self.log(f"recent score band: Danbooru check failed ({type(e).__name__}).")
            return None

        if not posts:
            self.call_telegram(
                "recent_score_band_no_posts",
                self.bot.send_message,
                chat_id,
                (
                    f"No encontre posts con {self.rating_filter}, score entre {min_score} y {max_score}, "
                    f"en las ultimas {hours} horas."
                ),
            )
            return 0

        return self.send_posts_to_chat(chat_id, posts)

    def send_top_score_posts(self, chat_id, min_score, limit):
        try:
            posts = self.fetch_top_score_posts(min_score=min_score, limit=limit)
        except Exception as error:
            self.log(f"top score command: Danbooru check failed ({type(error).__name__}).")
            return None

        if not posts:
            self.call_telegram(
                "top_score_no_posts",
                self.bot.send_message,
                chat_id,
                f"No encontre imagenes con {self.rating_filter} y score >= {min_score}.",
            )
            return 0

        return self.send_posts_to_chat(chat_id, posts)

    def send_hot_posts(self, chat_id, limit):
        try:
            posts = self.fetch_hot_posts(limit=limit)
        except Exception as error:
            self.log(f"hot command: Danbooru check failed ({type(error).__name__}).")
            return None

        if not posts:
            self.call_telegram(
                "hot_no_posts",
                self.bot.send_message,
                chat_id,
                f"No encontre imagenes para /hot con {self.hot_tag_query}.",
            )
            return 0

        return self.send_posts_to_chat(chat_id, posts)

    # ------------------------------------------------------------------
    # Polling cycle
    # ------------------------------------------------------------------

    def mark_handled(self, post_ids):
        def mutator(state):
            known = set(state["handled_post_ids"])

            for post_id in post_ids:
                if post_id not in known:
                    state["handled_post_ids"].append(post_id)
                    known.add(post_id)

            state["last_check_at"] = datetime.now(timezone.utc).isoformat()

        self.update_state(mutator)

    def record_danbooru_failure(self, description):
        # Solo lo llama monitor_loop (un unico hilo), por eso no lleva lock.
        self.consecutive_danbooru_failures += 1

        if (
            self.consecutive_danbooru_failures >= DANBOORU_FAILURE_ALERT_THRESHOLD
            and not self.danbooru_failure_alerted
        ):
            self.danbooru_failure_alerted = True
            self.send_operator_alert(
                f"⚠️ [{self.bot_name}] Danbooru falla hace {self.consecutive_danbooru_failures} ciclos seguidos "
                f"(ultimo: {description}). Sigo reintentando cada {self.poll_interval_seconds} s."
            )

    def record_danbooru_success(self):
        if self.danbooru_failure_alerted:
            self.send_operator_alert(
                f"✅ [{self.bot_name}] Danbooru volvio a responder despues de "
                f"{self.consecutive_danbooru_failures} ciclo(s) fallido(s)."
            )

        self.consecutive_danbooru_failures = 0
        self.danbooru_failure_alerted = False

    def polling_updated_since(self, state):
        # Con cursor solo se pagina hasta los posts actualizados antes del ultimo ciclo
        # completo; sin cursor (estado viejo o bootstrap) se piden todas las paginas.
        cursor = parse_post_datetime(state.get("updated_cursor"))

        if cursor is None:
            return None

        return cursor - timedelta(seconds=UPDATED_CURSOR_MARGIN_SECONDS)

    def advance_updated_cursor(self, cycle_started_at):
        def mutator(state):
            state["updated_cursor"] = cycle_started_at
            state["last_check_at"] = datetime.now(timezone.utc).isoformat()

        self.update_state(mutator)

    def check_danbooru_once(self, reason):
        cycle_started_at = datetime.now(timezone.utc).isoformat()
        state = self.get_state()
        current_filter_signature = self.filter_signature()
        updated_since = self.polling_updated_since(state)
        # Un estado sin cursor (anterior a la paginacion con corte, o ilegible) tambien
        # se re-bootstrapea: la pasada completa llega mas atras que el historial y
        # mandaria de golpe posts que nunca se vieron.
        needs_bootstrap = (
            not state["bootstrapped"]
            or state.get("filter_signature") != current_filter_signature
            or updated_since is None
        )

        if needs_bootstrap:
            updated_since = None

        try:
            posts = self.fetch_matching_posts(updated_since=updated_since)
        except urllib.error.HTTPError as e:
            self.log(f"{reason}: Danbooru HTTP error {e.code}.")
            self.record_danbooru_failure(f"HTTP {e.code}")
            return 0
        except Exception as e:
            self.log(f"{reason}: Danbooru check failed ({type(e).__name__}).")
            self.record_danbooru_failure(type(e).__name__)
            return 0

        self.record_danbooru_success()
        handled = set(state["handled_post_ids"])
        post_ids = [str(post["id"]) for post in posts]

        if needs_bootstrap:
            def bootstrap_mutator(state_to_update):
                state_to_update["bootstrapped"] = True
                state_to_update["filter_signature"] = current_filter_signature
                state_to_update["handled_post_ids"] = list(
                    dict.fromkeys(state_to_update["handled_post_ids"] + post_ids)
                )
                state_to_update["updated_cursor"] = cycle_started_at
                state_to_update["last_check_at"] = datetime.now(timezone.utc).isoformat()

            self.update_state(bootstrap_mutator)
            self.log(
                f"{reason}: bootstrapped {len(post_ids)} current matching post(s) for {current_filter_signature}."
            )
            return 0

        new_posts = [post for post in reversed(posts) if str(post["id"]) not in handled]
        sent_ids = []

        if not new_posts:
            self.advance_updated_cursor(cycle_started_at)
            self.log(f"{reason}: no new matching posts.")
            return 0

        had_failure = False

        for post in new_posts:
            result = self.send_post_to_subscribers(post)

            # Se persiste post a post (no al final del lote) para que un apagado a
            # mitad de ciclo no pierda el registro de lo ya enviado y lo reenvie
            # al reiniciar.
            if result == SEND_RESULT_SENT:
                sent_ids.append(str(post["id"]))
                self.log(f"{reason}: sent Danbooru post {post['id']} with score {post.get('score')}.")
                self.mark_handled([str(post["id"])])
            elif result == SEND_RESULT_SKIPPED:
                # Se da por visto: reintentarlo cada ciclo no cambiaria el resultado.
                self.log(f"{reason}: skipped Danbooru post {post['id']} (no photo-quality variant).")
                self.mark_handled([str(post["id"])])
            else:
                had_failure = True

        # Un fallo pasajero deja el cursor quieto: el proximo ciclo vuelve a paginar
        # hasta este punto y el post fallido sigue al alcance para reintentarlo.
        if not had_failure:
            self.advance_updated_cursor(cycle_started_at)

        return len(sent_ids)

    def monitor_loop(self):
        while True:
            self.check_danbooru_once("scheduled check")
            self.check_now_event.wait(self.poll_interval_seconds)
            self.check_now_event.clear()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def register_handlers(self):
        commands = (
            (["start", "subscribe"], self.handle_subscribe),
            (["unsubscribe"], self.handle_unsubscribe),
            (["stop"], self.handle_stop),
            (["check"], self.handle_check),
            (["test"], self.handle_test),
            (["last6", "g6"], self.handle_last6),
            (["top"], self.handle_top),
            (["hot"], self.handle_hot),
            (["help"], self.handle_help),
            (["blacklist"], self.handle_blacklist),
            (["status"], self.handle_status),
        )

        for command_names, handler in commands:
            self.bot.register_message_handler(handler, commands=command_names)

    def log_command(self, command, message):
        self.log(f"command /{command} from {message.from_user.id if message.from_user else '?'}")

    def describe_score_band(self):
        return f"{self.rating_filter} score {self.score_band_min}-{self.score_band_max} de las ultimas 6 horas"

    def handle_subscribe(self, message):
        if not self.is_allowed_user(message):
            return

        self.log_command("subscribe", message)
        chat_id = str(message.chat.id)

        def mutator(state):
            if chat_id not in state["subscriber_chat_ids"]:
                state["subscriber_chat_ids"].append(chat_id)
                return True

            return False

        added = self.update_state(mutator)
        text = (
            "Listo. Te avisare por aqui cuando encuentre posts de Danbooru "
            f"con {self.rating_filter} y score >= {self.score_threshold}."
            if added
            else "Ya estabas suscrito. Sigo vigilando Danbooru."
        )
        self.call_telegram("subscribe_ack", self.bot.send_message, message.chat.id, text)

    def handle_unsubscribe(self, message):
        if not self.is_allowed_user(message):
            return

        self.log_command("unsubscribe", message)
        chat_id = str(message.chat.id)

        def mutator(state):
            state["subscriber_chat_ids"] = [
                current_chat_id
                for current_chat_id in state["subscriber_chat_ids"]
                if current_chat_id != chat_id
            ]

        self.update_state(mutator)
        self.call_telegram(
            "unsubscribe_ack",
            self.bot.send_message,
            message.chat.id,
            "Desuscripto. Ya no te enviare avisos. Usa /subscribe para volver a activarlos.",
        )

    def handle_stop(self, message):
        if not self.is_allowed_user(message):
            return

        # /stop no desuscribe: en los demas bots del workspace significa "frenar algo en
        # curso", asi que este bot no le da el mismo significado que /unsubscribe.
        self.log_command("stop", message)
        self.call_telegram(
            "stop_noop_ack",
            self.bot.send_message,
            message.chat.id,
            "Este bot no tiene ninguna tirada en curso para frenar. Si queres dejar de recibir avisos, usa /unsubscribe.",
        )

    def handle_check(self, message):
        if not self.is_allowed_user(message):
            return

        self.log_command("check", message)
        self.call_telegram("check_ack", self.bot.send_message, message.chat.id, "Revisando Danbooru ahora.")
        self.check_now_event.set()

    def handle_test(self, message):
        if not self.is_allowed_user(message):
            return

        self.log_command("test", message)
        if self.send_latest_matching_post(message.chat.id):
            self.call_telegram("test_ack", self.bot.send_message, message.chat.id, "Test enviado.")
        else:
            self.call_telegram("test_failed", self.bot.send_message, message.chat.id, "No pude enviar el test.")

    def handle_last6(self, message):
        if not self.is_allowed_user(message):
            return

        self.log_command("last6", message)
        self.call_telegram(
            "last6_ack",
            self.bot.send_message,
            message.chat.id,
            (
                f"Buscando posts con {self.rating_filter}, score entre {self.score_band_min} "
                f"y {self.score_band_max}, de las ultimas 6 horas."
            ),
        )
        sent_count = self.send_recent_score_band_posts(message.chat.id, hours=6)

        if sent_count is None:
            self.call_telegram("last6_failed", self.bot.send_message, message.chat.id, "No pude consultar Danbooru.")
        elif sent_count > 0:
            self.call_telegram(
                "last6_done",
                self.bot.send_message,
                message.chat.id,
                f"Listo. Envie {sent_count} post(s).",
            )

    def handle_top(self, message):
        if not self.is_allowed_user(message):
            return

        self.log_command("top", message)
        self.call_telegram(
            "top_ack",
            self.bot.send_message,
            message.chat.id,
            (
                f"Buscando las ultimas {TOP_COMMAND_LIMIT} imagenes con {self.rating_filter} "
                f"y score >= {self.top_score_threshold}."
            ),
        )
        sent_count = self.send_top_score_posts(
            message.chat.id,
            min_score=self.top_score_threshold,
            limit=TOP_COMMAND_LIMIT,
        )

        if sent_count is None:
            self.call_telegram("top_failed", self.bot.send_message, message.chat.id, "No pude consultar Danbooru.")
        elif sent_count > 0:
            self.call_telegram(
                "top_done",
                self.bot.send_message,
                message.chat.id,
                f"Listo. Envie {sent_count} imagen(es).",
            )

    def handle_hot(self, message):
        if not self.is_allowed_user(message):
            return

        self.log_command("hot", message)
        self.call_telegram(
            "hot_ack",
            self.bot.send_message,
            message.chat.id,
            f"Buscando /hot con {self.hot_tag_query}.",
        )
        sent_count = self.send_hot_posts(
            message.chat.id,
            limit=HOT_COMMAND_LIMIT,
        )

        if sent_count is None:
            self.call_telegram("hot_failed", self.bot.send_message, message.chat.id, "No pude consultar Danbooru.")
        elif sent_count > 0:
            self.call_telegram(
                "hot_done",
                self.bot.send_message,
                message.chat.id,
                f"Listo. Envie {sent_count} imagen(es).",
            )

    def handle_help(self, message):
        if not self.is_allowed_user(message):
            return

        self.log_command("help", message)
        text = (
            f"<b>Comandos disponibles — {html.escape(self.display_name, quote=False)}</b>\n\n"
            "/subscribe o /start — activar notificaciones\n"
            "/unsubscribe — desactivar notificaciones\n"
            "/check — forzar revision inmediata de Danbooru\n"
            "/test — enviar el post mas reciente que coincida\n"
            f"/last6 o /g6 — posts {self.describe_score_band()}\n"
            "/top — ultimas imagenes con score alto\n"
            "/hot — tendencias del dia (order:rank)\n"
            f"/blacklist — listar tags bloqueados ({len(BLACKLISTED_TAGS)} activos)\n"
            "/blacklist test &lt;tag&gt; — ver si un tag esta bloqueado\n"
            "/status — estado del bot\n"
            "/help — este mensaje"
        )
        self.call_telegram("help_ack", self.bot.send_message, message.chat.id, text, parse_mode="HTML")

    def handle_blacklist(self, message):
        if not self.is_allowed_user(message):
            return

        self.log_command("blacklist", message)
        parts = (message.text or "").split(maxsplit=2)

        if len(parts) >= 3 and parts[1].lower() == "test":
            tag = parts[2].strip().lower()
            matched = [blocked for blocked in BLACKLISTED_TAGS if fnmatch.fnmatchcase(tag, blocked)]
            escaped_tag = html.escape(tag, quote=False)

            if matched:
                text = (
                    f"El tag <code>{escaped_tag}</code> ESTA bloqueado por: "
                    f"<code>{html.escape(', '.join(sorted(matched)), quote=False)}</code>"
                )
            else:
                text = f"El tag <code>{escaped_tag}</code> NO esta bloqueado."

            self.call_telegram("blacklist_test_ack", self.bot.send_message, message.chat.id, text, parse_mode="HTML")
            return

        sorted_tags = sorted(BLACKLISTED_TAGS)
        chunks = [sorted_tags[i:i + 50] for i in range(0, len(sorted_tags), 50)]
        header = f"<b>Blacklist {html.escape(self.display_name, quote=False)} ({len(BLACKLISTED_TAGS)} tags)</b>\n"
        self.call_telegram(
            "blacklist_list_ack",
            self.bot.send_message,
            message.chat.id,
            header + "\n".join(f"• <code>{html.escape(t, quote=False)}</code>" for t in chunks[0]),
            parse_mode="HTML",
        )
        for chunk in chunks[1:]:
            self.call_telegram(
                "blacklist_list_ack_cont",
                self.bot.send_message,
                message.chat.id,
                "\n".join(f"• <code>{html.escape(t, quote=False)}</code>" for t in chunk),
                parse_mode="HTML",
            )

    def handle_status(self, message):
        if not self.is_allowed_user(message):
            return

        self.log_command("status", message)
        state = self.get_state()
        text = (
            f"Suscriptores: {len(state['subscriber_chat_ids'])}\n"
            f"Posts procesados: {len(state['handled_post_ids'])}\n"
            f"Ultima revision: {state['last_check_at'] or 'nunca'}\n"
            f"Fallos seguidos de Danbooru: {self.consecutive_danbooru_failures}\n"
            f"Cooldown API activo: {'si' if self.sending_is_paused() else 'no'}\n"
            f"Filtro: {self.rating_filter} score >= {self.score_threshold}, "
            f"creados hace <= {self.max_post_age_days} dias\n"
            f"/top usa score >= {self.top_score_threshold}\n"
            f"/hot usa score >= {self.hot_score_threshold}, age:<=24h\n"
            f"/last6 o /g6 trae {self.describe_score_band()}\n"
            f"Blacklist: {len(BLACKLISTED_TAGS)} tags\n"
            f"Intervalo: {self.poll_interval_seconds} segundos"
        )
        self.call_telegram("status_ack", self.bot.send_message, message.chat.id, text)

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def run_bot(self):
        self.log(
            f"{self.bot_name} starting | "
            f"score>={self.score_threshold} | "
            f"max_age={self.max_post_age_days}d | "
            f"top>={self.top_score_threshold} | "
            f"hot>={self.hot_score_threshold} | "
            f"poll={self.poll_interval_seconds}s | "
            f"pages={self.danbooru_pages} | "
            f"max_dl={self.max_download_bytes // (1024 * 1024)}MB | "
            f"blacklist={len(BLACKLISTED_TAGS)} tags"
        )

        threading.Thread(target=self.monitor_loop, daemon=True).start()
        retry_delay = 5

        while True:
            try:
                self.bot.polling(non_stop=False, timeout=20, long_polling_timeout=20)
                retry_delay = 5
            except Exception as e:
                self.log(f"Polling error ({type(e).__name__}). Retrying in {retry_delay} seconds.")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 300)

    def main(self, argv=None):
        parser = argparse.ArgumentParser(description=f"Run {self.display_name}.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and filter posts without sending to Telegram or writing state.",
        )
        args = parser.parse_args(argv)

        if args.dry_run:
            print(f"[DRY RUN] {self.bot_name} — fetching matching posts, no Telegram calls or state writes.")
            try:
                posts = self.fetch_matching_posts()
            except Exception as exc:
                print(f"[DRY RUN] fetch failed: {exc}", file=sys.stderr)
                return 1

            state = self.get_state()
            handled = set(state["handled_post_ids"])
            new_posts = [p for p in posts if str(p["id"]) not in handled]

            print(
                f"[DRY RUN] total matching: {len(posts)} | already handled: {len(posts) - len(new_posts)} "
                f"| new: {len(new_posts)}"
            )
            for i, post in enumerate(new_posts[:10], start=1):
                print(f"[DRY RUN]   {i}. id={post['id']} score={post.get('score')} rating={post.get('rating')}")
            if len(new_posts) > 10:
                print(f"[DRY RUN]   ... and {len(new_posts) - 10} more")
            print("[DRY RUN] done — no changes made.")
            return 0

        self.run_bot()
        return 0


def create_notifier_bot(**config):
    return DanbooruNotifierApp(config)
