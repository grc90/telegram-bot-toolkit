"""Bot de Telegram que consulta un imageboard y notifica posts nuevos.

El bot mantiene un estado local para recordar qué posts ya procesó y así
evitar reenvíos entre reinicios o entre ciclos del polling de Danbooru.
"""

import argparse
import html
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone

import telebot
from requests import exceptions as requests_exceptions

from backup_state import find_latest_backup_for_file
from bot_logging import get_bot_logger
from config_env import get_required_env, validate_telegram_token
from tag_blacklist import BLACKLISTED_TAGS, has_blacklisted_tag
from danbooru_client import download_media_to_memory, json_request, validate_post
from json_storage import JsonStorageError, read_json_file, write_json_atomic

API_TOKEN = get_required_env("IMAGEBOARD_NOTIFIER_TELEGRAM_TOKEN")
validate_telegram_token("IMAGEBOARD_NOTIFIER_TELEGRAM_TOKEN", API_TOKEN)
BOT_NAME = "imageboard_notifier"
DISPLAY_NAME = "Imageboard Notifier"
STATE_FILE = os.path.join(os.path.dirname(__file__), "imageboard_notifier_state.json")

DANBOORU_BASE_URL = "https://danbooru.donmai.us"
SCORE_THRESHOLD = int(os.environ.get("IMAGEBOARD_NOTIFIER_SCORE_THRESHOLD", "10"))
DANBOORU_TAG_QUERY = f"rating:g score:>={SCORE_THRESHOLD} order:updated"
DANBOORU_LIMIT = 100
DANBOORU_PAGES = int(os.environ.get("IMAGEBOARD_NOTIFIER_DANBOORU_PAGES", "10"))
POLL_INTERVAL_SECONDS = int(os.environ.get("IMAGEBOARD_NOTIFIER_POLL_INTERVAL_SECONDS", "600"))
API_FAILURE_COOLDOWN = 300
MAX_STORED_IDS = 10000
MAX_DOWNLOAD_BYTES = int(os.environ.get("IMAGEBOARD_NOTIFIER_MAX_DOWNLOAD_BYTES", str(49 * 1024 * 1024)))
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024
# Limites de sendPhoto: Telegram rechaza con PHOTO_INVALID_DIMENSIONS si la
# suma ancho+alto pasa 10000 o si la relacion de aspecto supera 20:1.
TELEGRAM_PHOTO_MAX_TOTAL_DIMENSION = 10000
TELEGRAM_PHOTO_MAX_ASPECT_RATIO = 20
TOP_COMMAND_SCORE_THRESHOLD = int(os.environ.get("IMAGEBOARD_NOTIFIER_TOP_SCORE_THRESHOLD", "20"))
TOP_COMMAND_LIMIT = 50
HOT_COMMAND_SCORE_THRESHOLD = int(os.environ.get("IMAGEBOARD_NOTIFIER_HOT_SCORE_THRESHOLD", "10"))
HOT_COMMAND_LIMIT = 50
HOT_COMMAND_TAG_QUERY = f"order:rank rating:g score:>={HOT_COMMAND_SCORE_THRESHOLD} age:<=24h"
ALLOWED_USER_ID = get_required_env("BOTS_ALLOWED_USER_ID")

bot = telebot.TeleBot(API_TOKEN or "0:missing", parse_mode="HTML")
logger = get_bot_logger(BOT_NAME)

state_lock = threading.Lock()
log_lock = threading.Lock()
api_cooldown_until = 0
api_cooldown_lock = threading.Lock()
check_now_event = threading.Event()


def log(message):
    with log_lock:
        logger.info(message)


def send_operator_alert(message):
    try:
        bot.send_message(ALLOWED_USER_ID, message)
    except Exception as error:
        log(f"Could not send operator alert ({type(error).__name__}).")


def default_state():
    # Estado mínimo para arrancar incluso si el JSON no existe o está corrupto.
    return {
        "bootstrapped": False,
        "filter_signature": "",
        "handled_post_ids": [],
        "subscriber_chat_ids": [],
        "last_check_at": None,
    }


def filter_signature():
    blacklist = ",".join(sorted(BLACKLISTED_TAGS))
    return f"{DANBOORU_TAG_QUERY} blacklist:{blacklist}"


def load_state_unlocked():
    try:
        state = read_json_file(STATE_FILE, default=default_state(), expected_type=dict)
    except JsonStorageError as e:
        error_type = type(e).__name__
        log(f"Could not load state ({error_type}). Trying last backup.")
        backup_path = find_latest_backup_for_file(STATE_FILE)

        if backup_path:
            try:
                state = read_json_file(backup_path, default=default_state(), expected_type=dict)
                write_json_atomic(STATE_FILE, state)
                log(f"Restored state from backup {backup_path.parent.name}.")
                send_operator_alert(
                    f"⚠️ [{BOT_NAME}] Estado corrupto ({error_type}). "
                    f"Restaure el estado desde el backup {backup_path.parent.name}."
                )
            except Exception as backup_error:
                log(f"Could not load backup ({type(backup_error).__name__}). Starting fresh.")
                send_operator_alert(
                    f"\U0001f6a8 [{BOT_NAME}] Estado corrupto ({error_type}) y backup fallido "
                    f"({type(backup_error).__name__}). Reinicie el estado: perdiste suscriptores e historial."
                )
                state = default_state()
        else:
            log("No backup found. Starting fresh.")
            send_operator_alert(
                f"\U0001f6a8 [{BOT_NAME}] Estado corrupto ({error_type}). No hay backup disponible. "
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
        if chat_id == ALLOWED_USER_ID
    ]
    return fresh


def save_state_unlocked(state):
    # Limita el historial para que el archivo no crezca indefinidamente.
    state["handled_post_ids"] = state["handled_post_ids"][-MAX_STORED_IDS:]
    write_json_atomic(STATE_FILE, state)


def get_state():
    with state_lock:
        return load_state_unlocked()


def update_state(mutator):
    # Todas las escrituras del estado pasan por el mismo lock.
    with state_lock:
        state = load_state_unlocked()
        result = mutator(state)
        save_state_unlocked(state)
        return result


def is_allowed_user(message):
    return bool(getattr(message, "from_user", None)) and str(message.from_user.id) == ALLOWED_USER_ID


def sending_is_paused():
    with api_cooldown_lock:
        return time.time() < api_cooldown_until


def pause_sending(context, error):
    global api_cooldown_until

    with api_cooldown_lock:
        api_cooldown_until = time.time() + API_FAILURE_COOLDOWN

    log(
        f"{context}: transient Telegram failure "
        f"({describe_telegram_error(error)}). Pausing sends for {API_FAILURE_COOLDOWN} seconds."
    )


def describe_telegram_error(error):
    if isinstance(error, telebot.apihelper.ApiTelegramException):
        return f"{type(error).__name__} {error.error_code}: {error.description}"

    return type(error).__name__


def should_pause_for_telegram_error(error):
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
    transient_markers = (
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

    return error.error_code >= 500 or any(marker in description for marker in transient_markers)


def normalize_telegram_call(func, args, kwargs):
    if getattr(func, "__name__", None) != "send_message" or getattr(func, "__self__", None) is not bot:
        return args, kwargs

    if len(args) < 2 or "parse_mode" in kwargs or not isinstance(args[1], str):
        return args, kwargs

    normalized_args = list(args)
    normalized_args[1] = html.escape(normalized_args[1], quote=False)
    return tuple(normalized_args), kwargs


def call_telegram(context, func, *args, **kwargs):
    # Corta envíos temporalmente cuando Telegram empieza a fallar en cascada.
    if sending_is_paused():
        return None

    args, kwargs = normalize_telegram_call(func, args, kwargs)

    try:
        return func(*args, **kwargs)
    except Exception as e:
        if should_pause_for_telegram_error(e):
            pause_sending(context, e)
        else:
            log(f"{context} failed ({describe_telegram_error(e)}).")
        return None


TRANSIENT_SEND_ERRORS = (requests_exceptions.RequestException, TimeoutError)
SEND_RETRY_ATTEMPTS = 2
SEND_RETRY_BACKOFF_SECONDS = 3

PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
ANIMATION_EXTENSIONS = {"gif", "mp4"}

# Resultado de un envio: entregado, descartado a proposito (no hay variante que
# Telegram acepte como foto) o fallido por un problema pasajero. Solo el ultimo
# se vuelve a intentar en el proximo ciclo.
SEND_RESULT_SENT = "sent"
SEND_RESULT_SKIPPED = "skipped"
SEND_RESULT_FAILED = "failed"


def rewind_media_arguments(args, kwargs):
    # El intento fallido deja el stream consumido: sin rebobinar, el reintento
    # sube 0 bytes y Telegram responde "file must be non-empty".
    for value in (*args, *kwargs.values()):
        if hasattr(value, "seek") and hasattr(value, "read"):
            try:
                value.seek(0)
            except (OSError, ValueError):
                pass


def try_telegram_detailed(context, func, *args, **kwargs):
    # Igual que try_telegram, pero expone el error para distinguir un fallo
    # pasajero (reintentar en el proximo ciclo) de un rechazo definitivo.
    for attempt in range(SEND_RETRY_ATTEMPTS + 1):
        if attempt:
            rewind_media_arguments(args, kwargs)

        try:
            return func(*args, **kwargs), None
        except TRANSIENT_SEND_ERRORS as e:
            if attempt < SEND_RETRY_ATTEMPTS:
                time.sleep(SEND_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            log(f"{context} failed after {attempt + 1} attempts ({type(e).__name__}: {e}).")
            return None, e
        except Exception as e:
            log(f"{context} failed ({type(e).__name__}: {e}).")
            return None, e

    return None, None


def try_telegram(context, func, *args, **kwargs):
    result, _error = try_telegram_detailed(context, func, *args, **kwargs)
    return result


def danbooru_request(path, params):
    return json_request(
        DANBOORU_BASE_URL,
        path,
        params,
        user_agent=f"{BOT_NAME}/1.0 (personal Telegram notifier)",
        expected_type=list,
    )


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


def post_is_sendable(post):
    is_valid, reason = validate_post(post, allowed_ratings={"g"})

    if not is_valid:
        post_id = post.get("id") if isinstance(post, dict) else "unknown"
        log(f"Skipping Danbooru post {post_id}: {reason}.")
        return False

    return True


def post_score(post):
    try:
        return int(post.get("score") or 0)
    except (TypeError, ValueError):
        log(f"Skipping Danbooru post {post.get('id')}: invalid score.")
        return None


def download_media(url):
    return download_media_to_memory(
        url,
        referer=DANBOORU_BASE_URL,
        user_agent=f"{BOT_NAME}/1.0 (personal Telegram notifier)",
        max_bytes=MAX_DOWNLOAD_BYTES,
    )


def media_size(media):
    position = media.tell()
    media.seek(0, os.SEEK_END)
    size = media.tell()
    media.seek(position)
    return size


def fetch_posts_by_query(tag_query, pages=DANBOORU_PAGES, per_page_limit=DANBOORU_LIMIT):
    posts = []

    for page in range(1, pages + 1):
        page_posts = danbooru_request(
            "/posts.json",
            {
                "tags": tag_query,
                "limit": per_page_limit,
                "page": page,
            },
        )

        if not page_posts:
            break

        posts.extend(page_posts)

    return posts


def fetch_matching_posts():
    filtered = []
    seen_ids = set()

    for post in fetch_posts_by_query(DANBOORU_TAG_QUERY):
        if not post_is_sendable(post):
            continue

        post_id = str(post.get("id", ""))

        if not post_id or post_id in seen_ids:
            continue

        seen_ids.add(post_id)

        score = post_score(post)
        if score is None:
            continue

        # Revalida localmente rating, score y blacklist aunque ya vengan en la query.
        if (
            post.get("rating") == "g"
            and score >= SCORE_THRESHOLD
            and not has_blacklisted_tag(post)
        ):
            filtered.append(post)

    return filtered


def fetch_recent_score_band_posts(min_score=5, max_score=10, hours=6):
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - (hours * 60 * 60)
    tag_query = f"rating:g score:>={min_score} score:<={max_score} order:updated"
    filtered = []
    seen_ids = set()

    for post in fetch_posts_by_query(tag_query):
        if not post_is_sendable(post):
            continue

        post_id = str(post.get("id", ""))

        if not post_id or post_id in seen_ids or has_blacklisted_tag(post):
            continue

        seen_ids.add(post_id)

        if post.get("rating") != "g":
            continue

        score = post_score(post)

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


def fetch_top_score_posts(min_score, limit):
    tag_query = f"rating:g score:>={min_score} order:updated"
    filtered = []
    seen_ids = set()

    for post in fetch_posts_by_query(tag_query):
        if not post_is_sendable(post):
            continue

        post_id = str(post.get("id", ""))

        if not post_id or post_id in seen_ids or has_blacklisted_tag(post):
            continue

        seen_ids.add(post_id)

        if post.get("rating") != "g":
            continue

        score = post_score(post)

        if score is None or score < min_score:
            continue

        extension = (post.get("file_ext") or "").lower()

        if extension not in {"jpg", "jpeg", "png", "webp"}:
            continue

        filtered.append(post)

        if len(filtered) >= limit:
            break

    return filtered


def fetch_hot_posts(limit):
    filtered = []
    seen_ids = set()

    for post in fetch_posts_by_query(HOT_COMMAND_TAG_QUERY, pages=1, per_page_limit=limit):
        if not post_is_sendable(post):
            continue

        post_id = str(post.get("id", ""))

        if not post_id or post_id in seen_ids or has_blacklisted_tag(post):
            continue

        seen_ids.add(post_id)

        if post.get("rating") != "g":
            continue

        score = post_score(post)

        if score is None or score < HOT_COMMAND_SCORE_THRESHOLD:
            continue

        extension = (post.get("file_ext") or "").lower()

        if extension not in {"jpg", "jpeg", "png", "webp"}:
            continue

        filtered.append(post)

        if len(filtered) >= limit:
            break

    return filtered


def post_url(post):
    return f"{DANBOORU_BASE_URL}/posts/{post['id']}"


def normalize_source_url(source):
    if not source:
        return ""

    source = str(source).strip()

    try:
        parsed = urllib.parse.urlparse(source)
    except ValueError:
        return source

    host = parsed.netloc.lower()
    path = parsed.path

    pixiv_match = re.search(r"/(\d+)_p\d+\.[a-z0-9]+$", path, re.IGNORECASE)

    if pixiv_match and host.endswith("pximg.net"):
        return f"https://www.pixiv.net/artworks/{pixiv_match.group(1)}"

    return source


def media_urls(post):
    # Sin preview_file_url: un post que solo tenga thumbnail se descarta antes
    # de intentar enviarlo.
    urls = []

    for url in (post.get("file_url"), post.get("large_file_url")):
        if url and url not in urls:
            urls.append(url)

    return urls


def photo_media_urls(post):
    # Solo variantes de calidad: el original primero y el sample como respaldo.
    # preview_file_url queda fuera a proposito: es el thumbnail de 180x180.
    urls = []

    for url in (post.get("file_url"), post.get("large_file_url")):
        if url and url not in urls:
            urls.append(url)

    return urls


def media_url(post):
    urls = media_urls(post)
    return urls[0] if urls else None


def build_caption(post):
    source = normalize_source_url(post.get("source") or "")
    return html.escape(source or post_url(post), quote=False)


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


def send_post_as_photo(chat_id, post, caption):
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
            log(f"Post {post.get('id')}: skipping {image_url} ({rejection}).")
            continue

        try:
            downloaded_media = download_media(image_url)
        except Exception as e:
            log(f"download_media failed for post {post.get('id')} ({type(e).__name__}).")
            had_transient_failure = True
            continue

        downloaded_size = media_size(downloaded_media)

        if downloaded_size == 0:
            log(f"download_media returned an empty file for post {post.get('id')} ({image_url}).")
            had_transient_failure = True
            continue

        if downloaded_size > TELEGRAM_PHOTO_MAX_BYTES:
            last_rejection = f"{downloaded_size} bytes exceed the {TELEGRAM_PHOTO_MAX_BYTES} byte photo limit"
            log(f"Post {post.get('id')}: {image_url} exceeds the Telegram photo limit; trying a smaller variant.")
            continue

        sent, error = try_telegram_detailed(
            "upload_photo", bot.send_photo, chat_id, downloaded_media, caption=caption
        )

        if sent:
            return SEND_RESULT_SENT

        if error is not None and should_pause_for_telegram_error(error):
            had_transient_failure = True
        elif error is not None:
            last_rejection = describe_telegram_error(error)

    if had_transient_failure:
        log(f"Post {post.get('id')}: photo upload failed for now; will retry on the next check.")
        return SEND_RESULT_FAILED

    log(f"Post {post.get('id')}: no photo-quality variant fits Telegram's limits; skipping.")
    detail = f" ({last_rejection})" if last_rejection else ""
    call_telegram(
        "photo_limits_notice",
        bot.send_message,
        chat_id,
        f"Telegram no acepta este post como foto{detail}:\n{post_url(post)}",
    )
    return SEND_RESULT_SKIPPED


def send_post_to_chat(chat_id, post):
    urls = media_urls(post)
    url = urls[0] if urls else None
    caption = build_caption(post)

    if not url:
        log(f"Skipping post {post.get('id')}: no media URL.")
        return SEND_RESULT_SKIPPED

    extension = (post.get("file_ext") or url.rsplit(".", 1)[-1]).lower()

    if extension in PHOTO_EXTENSIONS:
        return send_post_as_photo(chat_id, post, caption)

    if extension not in ANIMATION_EXTENSIONS:
        log(f"Skipping post {post.get('id')}: .{extension} would need a document upload.")
        return SEND_RESULT_SKIPPED

    # gif/mp4 se envian como animacion o video (nunca como documento): primero
    # por URL y, si Telegram no la acepta, subiendo el archivo descargado.
    by_url_sender = bot.send_animation if extension == "gif" else bot.send_video

    if try_telegram(f"send_{extension}", by_url_sender, chat_id, url, caption=caption):
        return SEND_RESULT_SENT

    try:
        downloaded_media = download_media(url)
    except Exception as e:
        log(f"download_media failed for post {post.get('id')} ({type(e).__name__}).")
        return SEND_RESULT_FAILED

    if media_size(downloaded_media) == 0:
        log(f"download_media returned an empty file for post {post.get('id')} ({url}).")
        return SEND_RESULT_FAILED

    uploaded, error = try_telegram_detailed(
        f"upload_{extension}", by_url_sender, chat_id, downloaded_media, caption=caption
    )

    if uploaded:
        return SEND_RESULT_SENT

    if error is not None and should_pause_for_telegram_error(error):
        return SEND_RESULT_FAILED

    log(f"Skipping post {post.get('id')}: media could not be sent without using a document.")
    return SEND_RESULT_SKIPPED


def send_post_to_subscribers(post):
    state = get_state()
    chat_ids = state["subscriber_chat_ids"]

    if not chat_ids:
        log(f"Post {post['id']} matched, but there are no subscribers yet.")
        return SEND_RESULT_FAILED

    results = []

    # Pequeña pausa entre chats para no apilar requests seguidas a Telegram.
    for chat_id in chat_ids:
        results.append(send_post_to_chat(chat_id, post))
        time.sleep(1)

    if SEND_RESULT_SENT in results:
        return SEND_RESULT_SENT

    return SEND_RESULT_FAILED if SEND_RESULT_FAILED in results else SEND_RESULT_SKIPPED


def send_latest_matching_post(chat_id):
    try:
        posts = fetch_matching_posts()
    except Exception as e:
        log(f"manual test: Danbooru check failed ({type(e).__name__}).")
        return False

    if not posts:
        call_telegram(
            "test_no_posts",
            bot.send_message,
            chat_id,
            f"No encontre posts con rating:g y score >= {SCORE_THRESHOLD}.",
        )
        return False

    return send_post_to_chat(chat_id, posts[0]) == SEND_RESULT_SENT


def send_recent_score_band_posts(chat_id, min_score=5, max_score=10, hours=6):
    try:
        posts = fetch_recent_score_band_posts(min_score=min_score, max_score=max_score, hours=hours)
    except Exception as e:
        log(f"recent score band: Danbooru check failed ({type(e).__name__}).")
        return None

    if not posts:
        call_telegram(
            "recent_score_band_no_posts",
            bot.send_message,
            chat_id,
            (
                f"No encontre posts con rating:g, score entre {min_score} y {max_score}, "
                f"en las ultimas {hours} horas."
            ),
        )
        return 0

    sent_count = 0

    for post in posts:
        if send_post_to_chat(chat_id, post) == SEND_RESULT_SENT:
            sent_count += 1
            time.sleep(1)

    return sent_count


def send_top_score_posts(chat_id, min_score, limit):
    try:
        posts = fetch_top_score_posts(min_score=min_score, limit=limit)
    except Exception as error:
        log(f"top score command: Danbooru check failed ({type(error).__name__}).")
        return None

    if not posts:
        call_telegram(
            "top_score_no_posts",
            bot.send_message,
            chat_id,
            f"No encontre imagenes con rating:g y score >= {min_score}.",
        )
        return 0

    sent_count = 0

    for post in posts:
        if send_post_to_chat(chat_id, post) == SEND_RESULT_SENT:
            sent_count += 1
            time.sleep(1)

    return sent_count


def send_hot_posts(chat_id, limit):
    try:
        posts = fetch_hot_posts(limit=limit)
    except Exception as error:
        log(f"hot command: Danbooru check failed ({type(error).__name__}).")
        return None

    if not posts:
        call_telegram(
            "hot_no_posts",
            bot.send_message,
            chat_id,
            f"No encontre imagenes para /hot con order:rank rating:g score:>={HOT_COMMAND_SCORE_THRESHOLD} age:<=24h.",
        )
        return 0

    sent_count = 0

    for post in posts:
        if send_post_to_chat(chat_id, post) == SEND_RESULT_SENT:
            sent_count += 1
            time.sleep(1)

    return sent_count


def mark_handled(post_ids):
    def mutator(state):
        known = set(state["handled_post_ids"])

        for post_id in post_ids:
            if post_id not in known:
                state["handled_post_ids"].append(post_id)
                known.add(post_id)

        state["last_check_at"] = datetime.now(timezone.utc).isoformat()

    update_state(mutator)


def check_danbooru_once(reason):
    try:
        posts = fetch_matching_posts()
    except urllib.error.HTTPError as e:
        log(f"{reason}: Danbooru HTTP error {e.code}.")
        return 0
    except Exception as e:
        log(f"{reason}: Danbooru check failed ({type(e).__name__}).")
        return 0

    state = get_state()
    handled = set(state["handled_post_ids"])
    post_ids = [str(post["id"]) for post in posts]

    current_filter_signature = filter_signature()

    if not state["bootstrapped"] or state.get("filter_signature") != current_filter_signature:
        def bootstrap_mutator(state_to_update):
            state_to_update["bootstrapped"] = True
            state_to_update["filter_signature"] = current_filter_signature
            state_to_update["handled_post_ids"] = list(
                dict.fromkeys(state_to_update["handled_post_ids"] + post_ids)
            )
            state_to_update["last_check_at"] = datetime.now(timezone.utc).isoformat()

        update_state(bootstrap_mutator)
        log(f"{reason}: bootstrapped {len(post_ids)} current matching post(s) for {current_filter_signature}.")
        return 0

    new_posts = [post for post in reversed(posts) if str(post["id"]) not in handled]
    sent_ids = []
    skipped_ids = []

    for post in new_posts:
        result = send_post_to_subscribers(post)

        if result == SEND_RESULT_SENT:
            sent_ids.append(str(post["id"]))
            log(f"{reason}: sent Danbooru post {post['id']} with score {post.get('score')}.")
        elif result == SEND_RESULT_SKIPPED:
            # Se da por visto: reintentarlo cada ciclo no cambiaria el resultado.
            skipped_ids.append(str(post["id"]))
            log(f"{reason}: skipped Danbooru post {post['id']} (no photo-quality variant).")

    if sent_ids or skipped_ids:
        mark_handled(sent_ids + skipped_ids)
    else:
        mark_handled([])
        log(f"{reason}: no new matching posts.")

    return len(sent_ids)


def monitor_loop():
    while True:
        check_danbooru_once("scheduled check")
        check_now_event.wait(POLL_INTERVAL_SECONDS)
        check_now_event.clear()


@bot.message_handler(commands=["start", "subscribe"])
def handle_subscribe(message):
    if not is_allowed_user(message):
        return

    log(f"command /subscribe from {message.from_user.id if message.from_user else '?'}")
    chat_id = str(message.chat.id)

    def mutator(state):
        if chat_id not in state["subscriber_chat_ids"]:
            state["subscriber_chat_ids"].append(chat_id)
            return True

        return False

    added = update_state(mutator)
    text = (
        "Listo. Te avisare por aqui cuando encuentre posts de Danbooru "
        f"con rating:g y score >= {SCORE_THRESHOLD}."
        if added
        else "Ya estabas suscrito. Sigo vigilando Danbooru."
    )
    call_telegram("subscribe_ack", bot.send_message, message.chat.id, text)


@bot.message_handler(commands=["unsubscribe"])
def handle_unsubscribe(message):
    if not is_allowed_user(message):
        return

    log(f"command /unsubscribe from {message.from_user.id if message.from_user else '?'}")
    chat_id = str(message.chat.id)

    def mutator(state):
        state["subscriber_chat_ids"] = [
            current_chat_id
            for current_chat_id in state["subscriber_chat_ids"]
            if current_chat_id != chat_id
        ]

    update_state(mutator)
    call_telegram(
        "unsubscribe_ack",
        bot.send_message,
        message.chat.id,
        "Desuscripto. Ya no te enviare avisos. Usa /subscribe para volver a activarlos.",
    )


@bot.message_handler(commands=["stop"])
def handle_stop(message):
    if not is_allowed_user(message):
        return

    # /stop no desuscribe: en los demas bots del workspace significa "frenar algo en
    # curso", asi que este bot no le da el mismo significado que /unsubscribe.
    log(f"command /stop from {message.from_user.id if message.from_user else '?'}")
    call_telegram(
        "stop_noop_ack",
        bot.send_message,
        message.chat.id,
        "Este bot no tiene ninguna tirada en curso para frenar. Si queres dejar de recibir avisos, usa /unsubscribe.",
    )


@bot.message_handler(commands=["check"])
def handle_check(message):
    if not is_allowed_user(message):
        return

    log(f"command /check from {message.from_user.id if message.from_user else '?'}")
    call_telegram("check_ack", bot.send_message, message.chat.id, "Revisando Danbooru ahora.")
    check_now_event.set()


@bot.message_handler(commands=["test"])
def handle_test(message):
    if not is_allowed_user(message):
        return

    log(f"command /test from {message.from_user.id if message.from_user else '?'}")
    if send_latest_matching_post(message.chat.id):
        call_telegram("test_ack", bot.send_message, message.chat.id, "Test enviado.")
    else:
        call_telegram("test_failed", bot.send_message, message.chat.id, "No pude enviar el test.")


@bot.message_handler(commands=["last6", "g6"])
def handle_last6(message):
    if not is_allowed_user(message):
        return

    log(f"command /last6 from {message.from_user.id if message.from_user else '?'}")
    call_telegram(
        "last6_ack",
        bot.send_message,
        message.chat.id,
        "Buscando posts con rating:g, score entre 5 y 10, de las ultimas 6 horas.",
    )
    sent_count = send_recent_score_band_posts(message.chat.id, hours=6)

    if sent_count is None:
        call_telegram("last6_failed", bot.send_message, message.chat.id, "No pude consultar Danbooru.")
    elif sent_count > 0:
        call_telegram(
            "last6_done",
            bot.send_message,
            message.chat.id,
            f"Listo. Envie {sent_count} post(s).",
        )


@bot.message_handler(commands=["top"])
def handle_top(message):
    if not is_allowed_user(message):
        return

    log(f"command /top from {message.from_user.id if message.from_user else '?'}")
    call_telegram(
        "top_ack",
        bot.send_message,
        message.chat.id,
        (
            f"Buscando las ultimas {TOP_COMMAND_LIMIT} imagenes con rating:g "
            f"y score >= {TOP_COMMAND_SCORE_THRESHOLD}."
        ),
    )
    sent_count = send_top_score_posts(
        message.chat.id,
        min_score=TOP_COMMAND_SCORE_THRESHOLD,
        limit=TOP_COMMAND_LIMIT,
    )

    if sent_count is None:
        call_telegram("top_failed", bot.send_message, message.chat.id, "No pude consultar Danbooru.")
    elif sent_count > 0:
        call_telegram(
            "top_done",
            bot.send_message,
            message.chat.id,
            f"Listo. Envie {sent_count} imagen(es).",
        )


@bot.message_handler(commands=["hot"])
def handle_hot(message):
    if not is_allowed_user(message):
        return

    log(f"command /hot from {message.from_user.id if message.from_user else '?'}")
    call_telegram(
        "hot_ack",
        bot.send_message,
        message.chat.id,
        f"Buscando /hot con order:rank rating:g score:>={HOT_COMMAND_SCORE_THRESHOLD} age:<=24h.",
    )
    sent_count = send_hot_posts(
        message.chat.id,
        limit=HOT_COMMAND_LIMIT,
    )

    if sent_count is None:
        call_telegram("hot_failed", bot.send_message, message.chat.id, "No pude consultar Danbooru.")
    elif sent_count > 0:
        call_telegram(
            "hot_done",
            bot.send_message,
            message.chat.id,
            f"Listo. Envie {sent_count} imagen(es).",
        )


@bot.message_handler(commands=["help"])
def handle_help(message):
    if not is_allowed_user(message):
        return

    log(f"command /help from {message.from_user.id if message.from_user else '?'}")
    text = (
        f"<b>Comandos disponibles — {DISPLAY_NAME}</b>\n\n"
        "/subscribe o /start — activar notificaciones\n"
        "/unsubscribe — desactivar notificaciones\n"
        "/check — forzar revision inmediata de Danbooru\n"
        "/test — enviar el post mas reciente que coincida\n"
        "/last6 o /g6 — posts rating:g score 5-10 de las ultimas 6 horas\n"
        "/top — ultimas imagenes con score alto\n"
        "/hot — tendencias del dia (order:rank)\n"
        f"/blacklist — listar tags bloqueados ({len(BLACKLISTED_TAGS)} activos)\n"
        "/blacklist test &lt;tag&gt; — ver si un tag esta bloqueado\n"
        "/status — estado del bot\n"
        "/help — este mensaje"
    )
    call_telegram("help_ack", bot.send_message, message.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=["blacklist"])
def handle_blacklist(message):
    if not is_allowed_user(message):
        return

    log(f"command /blacklist from {message.from_user.id if message.from_user else '?'}")
    parts = (message.text or "").split(maxsplit=2)

    if len(parts) >= 3 and parts[1].lower() == "test":
        tag = parts[2].strip().lower()
        # Simulate a minimal post dict with the given tag in tag_string
        fake_post = {"tag_string": tag}
        matched = [
            blocked for blocked in BLACKLISTED_TAGS
            if __import__("fnmatch").fnmatchcase(tag, blocked)
        ]
        if matched:
            call_telegram(
                "blacklist_test_ack",
                bot.send_message,
                message.chat.id,
                f"El tag <code>{html.escape(tag, quote=False)}</code> ESTA bloqueado por: <code>{html.escape(', '.join(sorted(matched)), quote=False)}</code>",
                parse_mode="HTML",
            )
        else:
            call_telegram(
                "blacklist_test_ack",
                bot.send_message,
                message.chat.id,
                f"El tag <code>{html.escape(tag, quote=False)}</code> NO esta bloqueado.",
                parse_mode="HTML",
            )
        return

    sorted_tags = sorted(BLACKLISTED_TAGS)
    chunks = [sorted_tags[i:i + 50] for i in range(0, len(sorted_tags), 50)]
    header = f"<b>Blacklist {DISPLAY_NAME} ({len(BLACKLISTED_TAGS)} tags)</b>\n"
    call_telegram(
        "blacklist_list_ack",
        bot.send_message,
        message.chat.id,
        header + "\n".join(f"• <code>{html.escape(t, quote=False)}</code>" for t in chunks[0]),
        parse_mode="HTML",
    )
    for chunk in chunks[1:]:
        call_telegram(
            "blacklist_list_ack_cont",
            bot.send_message,
            message.chat.id,
            "\n".join(f"• <code>{html.escape(t, quote=False)}</code>" for t in chunk),
            parse_mode="HTML",
        )


@bot.message_handler(commands=["status"])
def handle_status(message):
    if not is_allowed_user(message):
        return

    log(f"command /status from {message.from_user.id if message.from_user else '?'}")
    state = get_state()
    text = (
        f"Suscriptores: {len(state['subscriber_chat_ids'])}\n"
        f"Posts procesados: {len(state['handled_post_ids'])}\n"
        f"Ultima revision: {state['last_check_at'] or 'nunca'}\n"
        f"Cooldown API activo: {'si' if sending_is_paused() else 'no'}\n"
        f"Filtro: rating:g score >= {SCORE_THRESHOLD}\n"
        f"/top usa score >= {TOP_COMMAND_SCORE_THRESHOLD}\n"
        f"/hot usa score >= {HOT_COMMAND_SCORE_THRESHOLD}, age:<=24h\n"
        "/last6 o /g6 trae rating:g con score 5-10 de las ultimas 6 hs\n"
        f"Blacklist: {len(BLACKLISTED_TAGS)} tags\n"
        f"Intervalo: {POLL_INTERVAL_SECONDS} segundos"
    )
    call_telegram("status_ack", bot.send_message, message.chat.id, text)


def run_bot():
    if not API_TOKEN:
        raise RuntimeError("Set IMAGEBOARD_NOTIFIER_TELEGRAM_TOKEN before running imageboard_notifier_bot.py")

    log(
        f"{BOT_NAME} starting | "
        f"score>={SCORE_THRESHOLD} | "
        f"top>={TOP_COMMAND_SCORE_THRESHOLD} | "
        f"hot>={HOT_COMMAND_SCORE_THRESHOLD} | "
        f"poll={POLL_INTERVAL_SECONDS}s | "
        f"pages={DANBOORU_PAGES} | "
        f"max_dl={MAX_DOWNLOAD_BYTES // (1024*1024)}MB | "
        f"blacklist={len(BLACKLISTED_TAGS)} tags"
    )

    threading.Thread(target=monitor_loop, daemon=True).start()
    retry_delay = 5

    while True:
        try:
            bot.polling(non_stop=False, timeout=20, long_polling_timeout=20)
            retry_delay = 5
        except Exception as e:
            log(f"Polling error ({type(e).__name__}). Retrying in {retry_delay} seconds.")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)


def main(argv=None):
    parser = argparse.ArgumentParser(description=f"Run {DISPLAY_NAME}.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and filter posts without sending to Telegram or writing state.",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        print(f"[DRY RUN] {BOT_NAME} — fetching matching posts, no Telegram calls or state writes.")
        try:
            posts = fetch_matching_posts()
        except Exception as exc:
            print(f"[DRY RUN] fetch failed: {exc}", file=sys.stderr)
            return 1

        state = get_state()
        handled = set(state["handled_post_ids"])
        new_posts = [p for p in posts if str(p["id"]) not in handled]

        print(f"[DRY RUN] total matching: {len(posts)} | already handled: {len(posts) - len(new_posts)} | new: {len(new_posts)}")
        for i, post in enumerate(new_posts[:10], start=1):
            print(f"[DRY RUN]   {i}. id={post['id']} score={post.get('score')} rating={post.get('rating')}")
        if len(new_posts) > 10:
            print(f"[DRY RUN]   ... and {len(new_posts) - 10} more")
        print("[DRY RUN] done — no changes made.")
        return 0

    run_bot()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
