"""Bot conversacional para traer los ultimos 100 posts de Danbooru.

Le pide al usuario autorizado:
- una tag
- un age
- un rating
- un order

Luego consulta Danbooru y envia hasta 100 posts al chat.
"""

import html
import os
import re
import threading
import time
import urllib.error

import telebot
from telebot import types

from bot_logging import get_bot_logger
from config_env import get_required_env
from tag_blacklist import BLACKLISTED_TAGS, has_blacklisted_tag
from danbooru_client import (
    DANBOORU_BASE_URL,
    PHOTO_EXTENSIONS,
    TELEGRAM_PHOTO_MAX_BYTES,
    TELEGRAM_PHOTO_URL_MAX_BYTES,
    build_caption,
    download_media_to_memory,
    json_request,
    media_size,
    media_url,
    photo_media_urls,
    photo_variant_metadata,
    photo_variant_rejection,
    validate_post,
)
from json_storage import JsonStorageError, read_json_file, write_json_atomic
from telegram_common import should_pause_for_telegram_error

API_TOKEN = get_required_env("IMAGEBOARD_SEARCH_TELEGRAM_TOKEN")
BOT_NAME = "imageboard_search"
BOT_VERSION = "2026-09-21-inline-query-1"
ALLOWED_USER_ID = get_required_env("BOTS_ALLOWED_USER_ID")
SESSION_STATE_FILE = os.path.join(os.path.dirname(__file__), "imageboard_search_state.json")

DANBOORU_LIMIT = 100
TAG_SUGGESTION_LIMIT = 8
TAG_SUGGESTION_FETCH_LIMIT = 30
INLINE_QUERY_CACHE_SECONDS = 30
MAX_DOWNLOAD_BYTES = int(os.environ.get("IMAGEBOARD_SEARCH_MAX_DOWNLOAD_BYTES", str(49 * 1024 * 1024)))
SEND_DELAY_SECONDS = float(os.environ.get("IMAGEBOARD_SEARCH_SEND_DELAY_SECONDS", "0.8"))

bot_username = ""

ALLOWED_RATINGS = {
    "g": "general",
    "s": "sensitive",
    "q": "questionable",
    "e": "explicit",
}

RATING_ALIASES = {
    "general": "g",
    "safe": "g",
    "sensitive": "s",
    "questionable": "q",
    "explicit": "e",
}

ALLOWED_ORDERS = {
    "updated",
    "score",
    "favcount",
    "comment",
    "rank",
    "id",
    "id_desc",
}

# Tags por busqueda para usuarios sin cuenta Gold (help:users). Los metatags de
# FREE_METATAGS no cuentan (help:cheatsheet); order: si cuenta.
DANBOORU_TAG_LIMIT = 2
FREE_METATAGS = {
    "rating", "status", "is", "age", "date", "id", "limit", "score", "downvotes",
    "favcount", "width", "height", "ratio", "mpixels", "filesize", "filetype",
    "duration", "md5", "pixiv_id", "pixiv", "parent", "child", "upvote",
    "embedded", "tagcount",
}

bot = telebot.TeleBot(API_TOKEN or "0:missing", parse_mode="HTML")
logger = get_bot_logger(BOT_NAME)

log_lock = threading.Lock()
session_lock = threading.Lock()
fetch_lock = threading.Lock()
fetch_state_lock = threading.Lock()
chat_sessions = {}
active_fetch = {
    "chat_id": None,
    "query": "",
    "stage": "idle",
    "stop_event": None,
}


def log(message):
    with log_lock:
        logger.info(message)


def send_text(chat_id, text):
    return bot.send_message(chat_id, html.escape(text, quote=False))


def is_allowed_user(message):
    return bool(getattr(message, "from_user", None)) and str(message.from_user.id) == ALLOWED_USER_ID


def normalize_tag_text(raw_value):
    return "_".join((raw_value or "").strip().split())


def danbooru_request(path, params):
    return json_request(
        DANBOORU_BASE_URL,
        path,
        params,
        user_agent=f"{BOT_NAME}/1.0 (personal Telegram fetcher)",
        expected_type=list,
    )


def download_media(url):
    return download_media_to_memory(
        url,
        referer=DANBOORU_BASE_URL,
        user_agent=f"{BOT_NAME}/1.0 (personal Telegram fetcher)",
        max_bytes=MAX_DOWNLOAD_BYTES,
    )


def try_send(context, func, *args, **kwargs):
    try:
        return func(*args, **kwargs), None
    except Exception as error:
        log(f"{context} failed ({type(error).__name__}: {error}).")
        return None, error


def _telegram_rejected_url(error):
    """True only for a definitive 4xx rejection; a 429 (flood control) is transient."""
    return (
        isinstance(error, telebot.apihelper.ApiTelegramException)
        and 400 <= error.error_code < 500
        and not should_pause_for_telegram_error(error)
    )


def send_photo_post(chat_id, post, caption):
    """Send a photo at the best quality Telegram accepts, never as a document.

    Same variant policy as the notifiers: original first, then the sample, never
    the 180x180 preview. Each variant goes by URL when Telegram can fetch it
    (<= 5 MB); otherwise, or if Telegram rejects the URL, it is downloaded and
    uploaded (<= 10 MB).
    """

    variant_metadata = photo_variant_metadata(post)
    fail_reason = "no_media_url"

    for image_url in photo_media_urls(post):
        width, height, file_size = variant_metadata.get(image_url, (None, None, None))

        if photo_variant_rejection(width, height, file_size):
            fail_reason = "photo_limits"
            continue

        if not file_size or file_size <= TELEGRAM_PHOTO_URL_MAX_BYTES:
            sent, error = try_send("send_photo", bot.send_photo, chat_id, image_url, caption=caption)

            if sent:
                return True, None

            # Sin un rechazo 4xx definitivo la foto pudo llegar igual (timeout):
            # subirla o probar otra variante la duplicaria.
            if not _telegram_rejected_url(error):
                return False, "send_failed"

        try:
            downloaded_media = download_media(image_url)
        except Exception as error:
            log(f"download_media failed for post {post.get('id')} ({type(error).__name__}: {error}).")
            fail_reason = "too_large" if "too large" in str(error) else "download_failed"
            continue

        if media_size(downloaded_media) > TELEGRAM_PHOTO_MAX_BYTES:
            fail_reason = "photo_limits"
            continue

        sent, error = try_send("upload_photo", bot.send_photo, chat_id, downloaded_media, caption=caption)

        if sent:
            return True, None

        if not _telegram_rejected_url(error):
            return False, "send_failed"

        fail_reason = "send_failed"

    return False, fail_reason


def send_post_to_chat(chat_id, post):
    """Send a post's media to chat.

    Returns (sent, fail_reason). fail_reason is None on success and a short
    code otherwise, used to build an actionable end-of-run summary for the user.
    """

    url = media_url(post)
    caption = build_caption(post)

    if not url:
        log(f"Skipping post {post.get('id')}: no media URL.")
        return False, "no_media_url"

    extension = (post.get("file_ext") or url.rsplit(".", 1)[-1]).lower()

    if extension in PHOTO_EXTENSIONS:
        return send_photo_post(chat_id, post, caption)

    url_send_attempted = False
    url_send_error = None

    if extension == "gif":
        url_send_attempted = True
        sent, url_send_error = try_send("send_animation", bot.send_animation, chat_id, url, caption=caption)
        if sent:
            return True, None

    if extension in {"mp4", "webm"}:
        url_send_attempted = True
        sent, url_send_error = try_send("send_video", bot.send_video, chat_id, url, caption=caption)
        if sent:
            return True, None

    # If we tried a URL send but didn't get a definitive 4xx rejection (e.g. timed out),
    # the message may already have been delivered — skip the download fallback to avoid duplicates.
    if url_send_attempted and not _telegram_rejected_url(url_send_error):
        return False, "send_failed"

    try:
        downloaded_media = download_media(url)
    except Exception as error:
        log(f"download_media failed for post {post.get('id')} ({type(error).__name__}: {error}).")
        reason = "too_large" if "too large" in str(error) else "download_failed"
        return False, reason

    if extension == "gif":
        sent, _ = try_send("upload_animation", bot.send_animation, chat_id, downloaded_media, caption=caption)
        if sent:
            return True, None

    downloaded_media.seek(0)

    if extension in {"mp4", "webm"}:
        sent, _ = try_send("upload_video", bot.send_video, chat_id, downloaded_media, caption=caption)
        if sent:
            return True, None

    downloaded_media.seek(0)
    sent, _ = try_send("upload_document", bot.send_document, chat_id, downloaded_media, caption=caption)
    return bool(sent), None if sent else "send_failed"


def normalize_rating(raw_value):
    value = (raw_value or "").strip().lower()

    if not value:
        return ""

    normalized_ratings = []

    for item in value.split(","):
        rating = RATING_ALIASES.get(item.strip(), item.strip())

        if rating not in ALLOWED_RATINGS:
            return ""

        if rating not in normalized_ratings:
            normalized_ratings.append(rating)

    return ",".join(normalized_ratings)


def normalize_order(raw_value):
    value = (raw_value or "").strip().lower()

    if value in ALLOWED_ORDERS:
        return value

    return ""


def persist_session_state(chat_id, session):
    """Best-effort disk checkpoint so a bot restart mid-flow isn't a silent dead end.

    Failures here must never break the interactive flow, so any I/O error is
    logged and swallowed.
    """

    try:
        if session:
            write_json_atomic(SESSION_STATE_FILE, {"chat_id": chat_id, "session": session})
        else:
            write_json_atomic(SESSION_STATE_FILE, {})
    except Exception as error:
        log(f"Could not persist session state ({type(error).__name__}: {error}).")


def reset_session(chat_id):
    with session_lock:
        chat_sessions[chat_id] = {
            "step": "tag",
            "tag": "",
            "age": "",
            "rating": "",
            "order": "",
            "tag_query": "",
            "tag_options": [],
            "tag_round": 0,
        }
        persist_session_state(chat_id, chat_sessions[chat_id])


def get_session(chat_id):
    with session_lock:
        return dict(chat_sessions.get(chat_id, {}))


def update_session(chat_id, **updates):
    with session_lock:
        session = chat_sessions.setdefault(
            chat_id,
            {
                "step": "tag",
                "tag": "",
                "age": "",
                "rating": "",
                "order": "",
                "tag_query": "",
                "tag_options": [],
                "tag_round": 0,
            },
        )
        session.update(updates)
        persist_session_state(chat_id, session)
        return dict(session)


def clear_session(chat_id):
    with session_lock:
        chat_sessions.pop(chat_id, None)
        persist_session_state(chat_id, None)


def clear_next_step(chat_id):
    try:
        bot.clear_step_handler_by_chat_id(chat_id)
    except Exception:
        pass


def set_active_fetch(chat_id=None, query="", stage="idle", stop_event=None):
    with fetch_state_lock:
        active_fetch["chat_id"] = chat_id
        active_fetch["query"] = query
        active_fetch["stage"] = stage
        active_fetch["stop_event"] = stop_event


def get_active_fetch():
    with fetch_state_lock:
        return dict(active_fetch)


def update_active_fetch_stage(stage):
    with fetch_state_lock:
        active_fetch["stage"] = stage


def command_name(text):
    normalized_text = (text or "").strip()

    if not normalized_text.startswith("/"):
        return ""

    first_token = normalized_text.split(None, 1)[0]
    return first_token[1:].split("@", 1)[0].lower()


def handle_in_flow_command(message):
    command = command_name(message.text)

    if not command:
        return False

    if command == "fetch":
        prompt_for_tag(message.chat.id)
        return True

    if command == "cancel":
        clear_next_step(message.chat.id)
        clear_session(message.chat.id)
        send_text(message.chat.id, "Busqueda cancelada. Usa /fetch para empezar de nuevo.")
        return True

    if command == "stop":
        current_fetch = get_active_fetch()

        if current_fetch.get("chat_id") != message.chat.id or not current_fetch.get("stop_event"):
            clear_next_step(message.chat.id)
            clear_session(message.chat.id)
            send_text(message.chat.id, "No hay una tirada en curso. He cancelado el flujo pendiente.")
            return True

        current_fetch["stop_event"].set()
        clear_next_step(message.chat.id)
        clear_session(message.chat.id)
        send_text(message.chat.id, "Parando la tirada actual. Corto en cuanto termine el post que este procesando.")
        return True

    if command == "help":
        handle_help(message)
        resume_current_step(message.chat.id)
        return True

    if command == "status":
        handle_status(message)
        resume_current_step(message.chat.id)
        return True

    return False


def resume_current_step(chat_id):
    """Re-arm the step handler for the flow that was interrupted by /help or /status.

    Telebot's next_step_handler consumes the incoming message before command
    handlers ever see it, so without re-registering here the flow would go
    silent after an in-flow /help or /status.
    """

    step_handlers = {
        "tag": handle_tag_step,
        "tag_form": handle_tag_step,
        "age": handle_age_step,
        "rating": handle_rating_step,
        "order": handle_order_step,
    }
    handler = step_handlers.get(get_session(chat_id).get("step"))

    if handler:
        bot.register_next_step_handler_by_chat_id(chat_id, handler)


def inline_usage_hint():
    if bot_username:
        return f"Tambien podes escribir @{bot_username} y la tag aca mismo para ver sugerencias en modo inline mientras escribis."

    return "Tambien podes invocar el modo inline de este bot (@usuario_del_bot tag) para ver sugerencias mientras escribis."


def prompt_for_tag(chat_id):
    clear_next_step(chat_id)
    update_session(chat_id, step="tag", tag="", age="", rating="", order="", tag_query="", tag_options=[])
    send_text(
        chat_id,
        (
            "Nueva busqueda iniciada. Enviame la tag.\n"
            "Te voy a sacar sugerencias de Danbooru y te las dejo en botones.\n"
            f"{inline_usage_hint()}"
        ),
    )
    bot.register_next_step_handler_by_chat_id(chat_id, handle_tag_step)


AGE_QUICK_OPTIONS = ["24h", "3d", "7d", "1mo", "3mo", "6mo", "1y", "3y", "10y"]
RATING_QUICK_OPTIONS = ["g", "s", "q", "e", "g,s", "s,q", "q,e", "s,q,e", "g,s,q,e"]
ORDER_QUICK_OPTIONS = ["updated", "score", "favcount", "comment", "rank", "id", "id_desc"]

# Steps that have a previous step to go back to via "< Volver".
STEPS_WITH_BACK = {"age", "rating", "order"}


def build_step_markup(prefix, options, current_step):
    markup = types.InlineKeyboardMarkup(row_width=2)

    for option in options:
        markup.add(types.InlineKeyboardButton(option, callback_data=f"{prefix}:{option}"))

    if current_step in STEPS_WITH_BACK:
        markup.add(types.InlineKeyboardButton("< Volver", callback_data=f"stepback:{current_step}"))

    return markup


def prompt_for_age(chat_id):
    clear_next_step(chat_id)
    update_session(chat_id, step="age")
    send_text(chat_id, "Perfecto. Ahora enviame el age, por ejemplo: 7d, <7d, 24h o 3mo. Tambien podes tocar un boton.")
    bot.send_message(
        chat_id,
        "Elegi un age o escribi el tuyo:",
        reply_markup=build_step_markup("agesel", AGE_QUICK_OPTIONS, "age"),
    )
    bot.register_next_step_handler_by_chat_id(chat_id, handle_age_step)


def prompt_for_rating(chat_id):
    clear_next_step(chat_id)
    update_session(chat_id, step="rating")
    send_text(
        chat_id,
        "Bien. Ahora enviame el rating: g, s, q, e o combinaciones como g,s o e,q. Tambien podes tocar un boton.",
    )
    bot.send_message(
        chat_id,
        "Elegi un rating o escribi el tuyo:",
        reply_markup=build_step_markup("ratingsel", RATING_QUICK_OPTIONS, "rating"),
    )
    bot.register_next_step_handler_by_chat_id(chat_id, handle_rating_step)


def prompt_for_order(chat_id):
    clear_next_step(chat_id)
    update_session(chat_id, step="order")
    send_text(
        chat_id,
        (
            "Bien. Ahora enviame el order, por ejemplo: updated, score, favcount, comment, rank, id o id_desc. "
            "Tambien podes tocar un boton."
        ),
    )
    bot.send_message(
        chat_id,
        "Elegi un order o escribi el tuyo:",
        reply_markup=build_step_markup("ordersel", ORDER_QUICK_OPTIONS, "order"),
    )
    bot.register_next_step_handler_by_chat_id(chat_id, handle_order_step)


def normalize_age(raw_value):
    value = (raw_value or "").strip().lower()

    if not value:
        return ""

    if value.startswith(".."):
        value = f"<={value[2:]}"

    if re.fullmatch(r"\d+[a-z]+", value):
        return f"<={value}"

    if re.fullmatch(r"[<>]=?\d+[a-z]+", value):
        return value

    return ""


def autocomplete_tags(query_text):
    """Query Danbooru's tag autocomplete and return ranked (name, post_count) pairs."""

    normalized_query = normalize_tag_text(query_text)

    if not normalized_query:
        return []

    suggestions_raw = danbooru_request(
        "/autocomplete.json",
        {
            "search[query]": normalized_query,
            "search[type]": "tag_query",
            "limit": TAG_SUGGESTION_FETCH_LIMIT,
        },
    )

    ranked_items = sorted(
        suggestions_raw,
        key=lambda item: (
            0 if str(item.get("value") or "").strip() == normalized_query else 1,
            -int(item.get("post_count") or 0),
            str(item.get("value") or ""),
        ),
    )

    seen = set()
    ranked = []

    for item in ranked_items:
        name = str(item.get("value") or "").strip()

        if name and name not in seen:
            seen.add(name)
            ranked.append((name, int(item.get("post_count") or 0)))

    return ranked


def search_tags(query_text):
    normalized_query = normalize_tag_text(query_text)
    suggestions = [name for name, _ in autocomplete_tags(query_text)]

    if normalized_query in suggestions:
        suggestions.remove(normalized_query)
        suggestions.insert(0, normalized_query)

    return suggestions[:TAG_SUGGESTION_LIMIT]


def build_tag_suggestions_markup(tag_query, suggestions, tag_round):
    """Build the suggestions keyboard, tagging every button with tag_round.

    tag_round changes each time a new suggestion set is sent, so a tap on a
    stale keyboard from an earlier refinement round is rejected instead of
    silently resolving against the current (different) tag_options/tag_query.
    """

    markup = types.InlineKeyboardMarkup(row_width=2)

    for index, tag_name in enumerate(suggestions):
        markup.add(types.InlineKeyboardButton(tag_name, callback_data=f"tagsel:{tag_round}:{index}"))

    if tag_query:
        markup.add(types.InlineKeyboardButton(f"Usar tal cual: {tag_query}", callback_data=f"taguse:{tag_round}"))

    return markup


def send_tag_suggestions(chat_id, tag_query, suggestions, tag_round):
    if suggestions:
        send_text(
            chat_id,
            (
                f"Sugerencias de Danbooru para: {tag_query}\n"
                "Toca una tag o manda otro texto para refinar la busqueda."
            ),
        )
    else:
        send_text(
            chat_id,
            (
                f"No encontre sugerencias de Danbooru para: {tag_query}\n"
                "Puedes usar la tag tal cual o mandar otro texto para seguir buscando."
            ),
        )

    try:
        bot.send_message(
            chat_id,
            "Selecciona una opcion:",
            reply_markup=build_tag_suggestions_markup(tag_query, suggestions, tag_round),
        )
    except Exception as error:
        log(f"send_tag_suggestions keyboard failed ({type(error).__name__}: {error}).")


def build_inline_tag_results(query_text):
    ranked = autocomplete_tags(query_text)
    results = []

    for index, (name, post_count) in enumerate(ranked[:TAG_SUGGESTION_LIMIT]):
        results.append(
            types.InlineQueryResultArticle(
                id=str(index),
                title=name,
                description=f"{post_count} post(s) en Danbooru",
                input_message_content=types.InputTextMessageContent(name),
            )
        )

    return results


@bot.inline_handler(func=lambda inline_query: True)
def handle_inline_tag_query(inline_query):
    """Live tag autocomplete via Telegram's native inline mode.

    Typing "@<bot_username> <partial tag>" (including in the chat with this
    bot itself) triggers this on every keystroke; picking a result sends it
    as a plain text message, which the normal tag-step flow then consumes
    exactly like a typed tag.
    """

    if str(inline_query.from_user.id) != ALLOWED_USER_ID:
        try_send("answer_inline_query", bot.answer_inline_query, inline_query.id, [], cache_time=1)
        return

    query_text = (inline_query.query or "").strip()

    if not query_text:
        try_send("answer_inline_query", bot.answer_inline_query, inline_query.id, [], cache_time=1)
        return

    try:
        results = build_inline_tag_results(query_text)
    except Exception as error:
        log(f"inline tag query failed ({type(error).__name__}: {error}).")
        results = []

    try_send(
        "answer_inline_query",
        bot.answer_inline_query,
        inline_query.id,
        results,
        cache_time=INLINE_QUERY_CACHE_SECONDS,
        is_personal=True,
    )


def count_limited_tags(tags):
    # Lo que Danbooru suma al limite: tags normales (con -, ~ o comodin) y
    # metatags no gratuitos.
    count = 0

    for term in (tags or "").split():
        prefix, separator, _ = term.lstrip("-~").partition(":")

        if separator and prefix.lower() in FREE_METATAGS:
            continue

        count += 1

    return count


def order_fits_tag_limit(tag):
    return count_limited_tags(tag) < DANBOORU_TAG_LIMIT


def build_query(tag, age, rating, order):
    # order: cuenta para el limite: con 2 tags no entra y queda el orden por
    # defecto de Danbooru (los mas nuevos primero).
    query = f"{tag} age:{age} rating:{rating}"
    return f"{query} order:{order}" if order_fits_tag_limit(tag) else query


def tag_has_wildcard(tag):
    return "*" in (tag or "")


def allowed_rating_set(rating):
    if not rating:
        return set(ALLOWED_RATINGS)

    return {item for item in rating.split(",") if item}


def post_is_fetchable(post, rating):
    is_valid, reason = validate_post(post, allowed_ratings=allowed_rating_set(rating))

    if not is_valid:
        post_id = post.get("id") if isinstance(post, dict) else "unknown"
        log(f"Skipping Danbooru post {post_id}: {reason}.")
        return False

    return True


def new_filter_stats():
    return {"raw": 0, "blacklisted": 0, "invalid": 0}


def add_filtered_post(filtered, seen_ids, post, rating, stats):
    if not post_is_fetchable(post, rating):
        stats["invalid"] += 1
        return

    post_id = str(post.get("id", ""))

    if not post_id or post_id in seen_ids:
        return

    if has_blacklisted_tag(post):
        stats["blacklisted"] += 1
        return

    seen_ids.add(post_id)
    filtered.append(post)


def fetch_posts_for_query(query, rating):
    posts = danbooru_request(
        "/posts.json",
        {
            "tags": query,
            "limit": DANBOORU_LIMIT,
        },
    )

    filtered = []
    seen_ids = set()
    stats = new_filter_stats()
    stats["raw"] = len(posts)

    for post in posts:
        add_filtered_post(filtered, seen_ids, post, rating, stats)

    return filtered, stats


def fetch_posts_from_wildcard_tag(tag, age, rating, order):
    suggestions = [suggestion for suggestion in search_tags(tag) if not tag_has_wildcard(suggestion)]
    stats = new_filter_stats()

    if not suggestions:
        return [], stats

    filtered = []
    seen_ids = set()

    for suggestion in suggestions:
        query = build_query(suggestion, age, rating, order)
        log(f"wildcard fallback query: {query}")

        posts = danbooru_request("/posts.json", {"tags": query, "limit": DANBOORU_LIMIT})
        stats["raw"] += len(posts)

        for post in posts:
            add_filtered_post(filtered, seen_ids, post, rating, stats)

            if len(filtered) >= DANBOORU_LIMIT:
                return filtered, stats

    return filtered, stats


def fetch_posts(tag, age, rating, order):
    query = build_query(tag, age, rating, order)

    try:
        return fetch_posts_for_query(query, rating)
    except urllib.error.HTTPError as error:
        if error.code not in {500, 502, 503, 504} or not tag_has_wildcard(tag):
            raise

        log(f"primary wildcard query failed with HTTP {error.code}; retrying exact autocomplete tags.")
        fallback_posts, fallback_stats = fetch_posts_from_wildcard_tag(tag, age, rating, order)

        if fallback_posts:
            return fallback_posts, fallback_stats

        raise


def fetch_error_message(error, tag):
    if isinstance(error, urllib.error.HTTPError):
        message = f"Danbooru devolvio HTTP {error.code} en este intento."

        if error.code >= 500 and tag_has_wildcard(tag):
            return (
                f"{message}\n"
                "Esa busqueda con comodin parece demasiado pesada. Prueba con una tag exacta, un age mas corto "
                "o un order como id_desc."
            )

        return f"{message}\nProba de nuevo en un rato con /fetch. Si persiste, cambia la tag, el age o el order."

    return "No pude consultar Danbooru en este intento.\nProba de nuevo en un rato con /fetch."


def describe_empty_result(stats):
    raw = stats.get("raw", 0)
    blacklisted = stats.get("blacklisted", 0)
    invalid = stats.get("invalid", 0)

    if raw == 0:
        return (
            "No encontre posts para esa combinacion.\n"
            "Puede ser que la tag no tenga posts con ese rating o age, o que este mal escrita. "
            "Proba con /fetch de nuevo para ver sugerencias, un age mas largo u otro rating."
        )

    if blacklisted and blacklisted + invalid >= raw:
        return (
            f"Encontre {raw} post(s) para esa busqueda, pero quedaron bloqueados por el filtro de contenido de este "
            f"bot ({blacklisted} filtrado(s)). No hay nada para mostrarte con esa tag."
        )

    return (
        f"Danbooru devolvio {raw} post(s) pero ninguno paso los filtros (rating, media faltante o bloqueo de "
        "contenido). Proba con otro rating o age."
    )


FAILURE_REASON_LABELS = {
    "too_large": "el archivo pesaba mas de lo permitido",
    "photo_limits": "Telegram no lo acepta como foto (peso o dimensiones)",
    "download_failed": "no pude descargar el archivo",
    "send_failed": "Telegram rechazo el envio",
    "no_media_url": "el post no tenia una URL de imagen o video",
}


def describe_failures(failures):
    if not failures:
        return ""

    counts = {}

    for reason in failures:
        counts[reason] = counts.get(reason, 0) + 1

    parts = [f"{count} por {FAILURE_REASON_LABELS.get(reason, reason)}" for reason, count in counts.items()]
    return f" No se pudieron mandar {len(failures)}: {', '.join(parts)}."


def start_fetch(chat_id, tag, age, rating, order):
    def worker():
        if not fetch_lock.acquire(blocking=False):
            send_text(chat_id, "Ya hay una busqueda en curso. Espera a que termine y vuelve a intentar.")
            return

        stop_event = threading.Event()

        try:
            query = build_query(tag, age, rating, order)
            set_active_fetch(chat_id=chat_id, query=query, stage="fetching", stop_event=stop_event)
            send_text(
                chat_id,
                (
                    "Buscando en Danbooru...\n"
                    f"tag: {tag}\n"
                    f"age: {age}\n"
                    f"rating: {rating}\n"
                    f"order: {order if order_fits_tag_limit(tag) else 'por defecto (con 2 tags Danbooru no deja sumar order:)'}\n"
                    f"query: {query}"
                ),
            )

            posts, stats = fetch_posts(tag, age, rating, order)

            if stop_event.is_set():
                send_text(chat_id, "Busqueda detenida antes de empezar a enviar posts.")
                return

            if not posts:
                send_text(chat_id, describe_empty_result(stats))
                return

            update_active_fetch_stage("sending")
            found_message = f"Encontre {len(posts)} post(s). Voy enviando hasta {DANBOORU_LIMIT}."

            if stats.get("blacklisted"):
                found_message += f" ({stats['blacklisted']} mas quedaron afuera por el filtro de contenido.)"

            send_text(chat_id, found_message)

            sent_count = 0
            failures = []

            for index, post in enumerate(posts, start=1):
                if stop_event.is_set():
                    send_text(chat_id, f"Tirada detenida. Envie {sent_count} de {len(posts)} post(s).")
                    return

                sent, fail_reason = send_post_to_chat(chat_id, post)

                if sent:
                    sent_count += 1
                else:
                    failures.append(fail_reason or "send_failed")

                if stop_event.is_set():
                    send_text(chat_id, f"Tirada detenida. Envie {sent_count} de {len(posts)} post(s).")
                    return

                if index < len(posts) and index % 10 == 0:
                    send_text(chat_id, f"Progreso: {index}/{len(posts)} procesados, {sent_count} enviados.")

                time.sleep(SEND_DELAY_SECONDS)

            send_text(
                chat_id,
                f"Listo. Envie {sent_count} de {len(posts)} post(s).{describe_failures(failures)}\n"
                f"query: {query}",
            )
        except Exception as error:
            log(f"fetch worker failed ({type(error).__name__}: {error}).")
            send_text(chat_id, fetch_error_message(error, tag))
        finally:
            set_active_fetch()
            fetch_lock.release()

    threading.Thread(target=worker, daemon=True).start()


def multi_tag_combined_form(text):
    """Join whitespace-separated tokens with single spaces (Danbooru AND syntax)."""

    return " ".join(text.split())


def send_tag_form_choice(chat_id, raw_text, compound, combined):
    send_text(
        chat_id,
        (
            f"Mandaste varias palabras: {raw_text}\n"
            "¿Es una sola tag o varias tags combinadas (AND)?"
        ),
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(f"Una sola tag: {compound}", callback_data="tagform:compound"))
    markup.add(types.InlineKeyboardButton(f"Combinadas (AND): {combined}", callback_data="tagform:combined"))
    bot.send_message(chat_id, "Elegi una opcion:", reply_markup=markup)


def proceed_with_tag_query(chat_id, normalized_tag):
    """Look up suggestions for a single-tag query and show them, or fall back to using it as-is."""

    try:
        suggestions = search_tags(normalized_tag)
    except Exception as error:
        log(f"tag search failed ({type(error).__name__}: {error}).")
        send_text(
            chat_id,
            (
                "No pude sacar sugerencias de Danbooru ahora mismo.\n"
                f"Voy a usar la tag tal cual: {normalized_tag}"
            ),
        )
        update_session(chat_id, tag=normalized_tag, tag_query="", tag_options=[])
        prompt_for_age(chat_id)
        return

    tag_round = update_session(chat_id, step="tag", tag="").get("tag_round", 0) + 1
    update_session(chat_id, tag_query=normalized_tag, tag_options=suggestions, tag_round=tag_round)
    send_tag_suggestions(chat_id, normalized_tag, suggestions, tag_round)
    bot.register_next_step_handler_by_chat_id(chat_id, handle_tag_step)


def handle_tag_step(message):
    if not is_allowed_user(message):
        return

    if handle_in_flow_command(message):
        return

    text = (message.text or "").strip()
    log(f"tag step received: {text!r}")

    if not text:
        prompt_for_tag(message.chat.id)
        return

    if len(text.split()) > 1:
        compound = normalize_tag_text(text)
        combined = multi_tag_combined_form(text)
        update_session(message.chat.id, step="tag_form", tag_form_compound=compound, tag_form_combined=combined)
        send_tag_form_choice(message.chat.id, text, compound, combined)
        bot.register_next_step_handler_by_chat_id(message.chat.id, handle_tag_step)
        return

    proceed_with_tag_query(message.chat.id, normalize_tag_text(text))


@bot.callback_query_handler(func=lambda call: call.data.startswith("tagform:"))
def handle_tag_form_callback(call):
    if not is_allowed_user(call):
        return

    chat_id = call.message.chat.id
    session = get_session(chat_id)

    if session.get("step") != "tag_form":
        try_send("answer_callback_query", bot.answer_callback_query, call.id, "Ya no estoy esperando esa eleccion.")
        return

    try_send(
        "edit_message_reply_markup",
        bot.edit_message_reply_markup,
        chat_id,
        call.message.message_id,
        reply_markup=None,
    )

    if call.data == "tagform:combined":
        combined = session.get("tag_form_combined") or ""
        combined_count = count_limited_tags(combined)

        if combined_count > DANBOORU_TAG_LIMIT:
            try_send("answer_callback_query", bot.answer_callback_query, call.id, "Demasiadas tags.")
            send_text(
                chat_id,
                (
                    f"Danbooru sin cuenta Gold permite hasta {DANBOORU_TAG_LIMIT} tags por busqueda "
                    f"y mandaste {combined_count}. Proba con menos."
                ),
            )
            prompt_for_tag(chat_id)
            return

        try_send("answer_callback_query", bot.answer_callback_query, call.id, f"Tags combinadas: {combined}")
        update_session(chat_id, step="tag", tag=combined, tag_query="", tag_options=[])
        send_text(chat_id, f"Tags combinadas: {combined}")
        prompt_for_age(chat_id)
        return

    try_send("answer_callback_query", bot.answer_callback_query, call.id, "Buscando sugerencias...")
    proceed_with_tag_query(chat_id, session.get("tag_form_compound") or "")


def handle_age_step(message):
    if not is_allowed_user(message):
        return

    if handle_in_flow_command(message):
        return

    text = (message.text or "").strip()
    log(f"age step received: {text!r}")
    age = normalize_age(text)

    if not age:
        send_text(message.chat.id, "Age invalido. Prueba con algo como 7d, <7d, 24h o 3mo.")
        bot.register_next_step_handler_by_chat_id(message.chat.id, handle_age_step)
        return

    update_session(message.chat.id, age=age)
    prompt_for_rating(message.chat.id)


def handle_rating_step(message):
    if not is_allowed_user(message):
        return

    if handle_in_flow_command(message):
        return

    text = (message.text or "").strip()
    log(f"rating step received: {text!r}")
    rating = normalize_rating(text)

    if not rating:
        send_text(
            message.chat.id,
            "Rating invalido. Usa g, s, q, e o combinaciones separadas por coma como g,s o e,q.",
        )
        bot.register_next_step_handler_by_chat_id(message.chat.id, handle_rating_step)
        return

    update_session(message.chat.id, rating=rating)
    prompt_for_order(message.chat.id)


def handle_order_step(message):
    if not is_allowed_user(message):
        return

    if handle_in_flow_command(message):
        return

    text = (message.text or "").strip()
    log(f"order step received: {text!r}")
    order = normalize_order(text)

    if not order:
        send_text(
            message.chat.id,
            "Order invalido. Prueba con updated, score, favcount, comment, rank, id o id_desc.",
        )
        bot.register_next_step_handler_by_chat_id(message.chat.id, handle_order_step)
        return

    finished = update_session(message.chat.id, step="done", order=order)
    clear_session(message.chat.id)
    start_fetch(message.chat.id, finished["tag"], finished["age"], finished["rating"], finished["order"])


def dismiss_markup(chat_id, message_id):
    try_send(
        "edit_message_reply_markup",
        bot.edit_message_reply_markup,
        chat_id,
        message_id,
        reply_markup=None,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("agesel:"))
def handle_age_callback(call):
    if not is_allowed_user(call):
        return

    chat_id = call.message.chat.id
    session = get_session(chat_id)

    if session.get("step") != "age":
        try_send("answer_callback_query", bot.answer_callback_query, call.id, "Ya no estoy esperando un age.")
        return

    age = normalize_age(call.data.split(":", 1)[1])

    if not age:
        try_send("answer_callback_query", bot.answer_callback_query, call.id, "Age invalido.")
        return

    try_send("answer_callback_query", bot.answer_callback_query, call.id, f"Age elegido: {age}")
    dismiss_markup(chat_id, call.message.message_id)
    update_session(chat_id, age=age)
    send_text(chat_id, f"Age elegido: {age}")
    prompt_for_rating(chat_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("ratingsel:"))
def handle_rating_callback(call):
    if not is_allowed_user(call):
        return

    chat_id = call.message.chat.id
    session = get_session(chat_id)

    if session.get("step") != "rating":
        try_send("answer_callback_query", bot.answer_callback_query, call.id, "Ya no estoy esperando un rating.")
        return

    rating = normalize_rating(call.data.split(":", 1)[1])

    if not rating:
        try_send("answer_callback_query", bot.answer_callback_query, call.id, "Rating invalido.")
        return

    try_send("answer_callback_query", bot.answer_callback_query, call.id, f"Rating elegido: {rating}")
    dismiss_markup(chat_id, call.message.message_id)
    update_session(chat_id, rating=rating)
    send_text(chat_id, f"Rating elegido: {rating}")
    prompt_for_order(chat_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("ordersel:"))
def handle_order_callback(call):
    if not is_allowed_user(call):
        return

    chat_id = call.message.chat.id
    session = get_session(chat_id)

    if session.get("step") != "order":
        try_send("answer_callback_query", bot.answer_callback_query, call.id, "Ya no estoy esperando un order.")
        return

    order = normalize_order(call.data.split(":", 1)[1])

    if not order:
        try_send("answer_callback_query", bot.answer_callback_query, call.id, "Order invalido.")
        return

    try_send("answer_callback_query", bot.answer_callback_query, call.id, f"Order elegido: {order}")
    dismiss_markup(chat_id, call.message.message_id)
    clear_next_step(chat_id)
    finished = update_session(chat_id, step="done", order=order)
    clear_session(chat_id)
    start_fetch(chat_id, finished["tag"], finished["age"], finished["rating"], finished["order"])


@bot.callback_query_handler(func=lambda call: call.data.startswith("stepback:"))
def handle_step_back_callback(call):
    if not is_allowed_user(call):
        return

    chat_id = call.message.chat.id
    session = get_session(chat_id)
    current_step = call.data.split(":", 1)[1]

    if session.get("step") != current_step:
        try_send("answer_callback_query", bot.answer_callback_query, call.id, "Ya avanzaste de paso.")
        return

    back_targets = {
        "age": prompt_for_tag,
        "rating": prompt_for_age,
        "order": prompt_for_rating,
    }
    back_target = back_targets.get(current_step)

    if not back_target:
        try_send("answer_callback_query", bot.answer_callback_query, call.id, "No puedo volver desde aca.")
        return

    try_send("answer_callback_query", bot.answer_callback_query, call.id, "Volviendo al paso anterior...")
    dismiss_markup(chat_id, call.message.message_id)
    back_target(chat_id)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("tagsel:") or call.data.startswith("taguse:")
)
def handle_tag_callback(call):
    if not is_allowed_user(call):
        return

    session = get_session(call.message.chat.id)

    if session.get("step") != "tag":
        try_send("answer_callback_query", bot.answer_callback_query, call.id, "Ya no estoy esperando una tag.")
        return

    tag_query = session.get("tag_query") or ""
    tag_options = session.get("tag_options") or []
    current_round = session.get("tag_round", 0)
    selected_tag = ""

    if call.data.startswith("taguse:"):
        try:
            call_round = int(call.data.split(":", 1)[1])
        except ValueError:
            call_round = -1

        if call_round == current_round:
            selected_tag = tag_query
    else:
        try:
            _, call_round_str, index_str = call.data.split(":", 2)
            call_round = int(call_round_str)
            selected_index = int(index_str)
        except ValueError:
            call_round = -1
            selected_index = -1

        if call_round == current_round and 0 <= selected_index < len(tag_options):
            selected_tag = tag_options[selected_index]

    if not selected_tag:
        try_send("answer_callback_query", bot.answer_callback_query, call.id, "Esa sugerencia ya no es valida.")
        return

    try_send("answer_callback_query", bot.answer_callback_query, call.id, f"Tag elegida: {selected_tag}")
    try_send(
        "edit_message_reply_markup",
        bot.edit_message_reply_markup,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=None,
    )
    update_session(call.message.chat.id, tag=selected_tag, tag_query="", tag_options=[])
    send_text(call.message.chat.id, f"Tag elegida: {selected_tag}")
    prompt_for_age(call.message.chat.id)


@bot.message_handler(commands=["start"])
def handle_start(message):
    if not is_allowed_user(message):
        return

    send_text(
        message.chat.id,
        (
            "Danbooru Fetcher listo.\n"
            "Te voy a pedir una tag, un age, un rating y un order para traer los ultimos 100 posts.\n"
            "Enviame primero la tag y te sacare sugerencias desde Danbooru en botones.\n"
            f"{inline_usage_hint()}\n"
            "Comandos utiles: /fetch reinicia la busqueda, /cancel cancela el flujo, /stop corta una tirada en curso, /help muestra ayuda."
        ),
    )
    prompt_for_tag(message.chat.id)


@bot.message_handler(commands=["help"])
def handle_help(message):
    if not is_allowed_user(message):
        return

    send_text(
        message.chat.id,
        (
            "Uso:\n"
            "1. /fetch\n"
            "2. manda una tag o parte de una tag, por ejemplo: remilia\n"
            "3. elige una sugerencia de Danbooru (en botones) o usa la tag tal cual\n"
            f"   {inline_usage_hint()}\n"
            "4. manda un age (7d, <7d, 24h, 3mo) o toca un boton rapido\n"
            "5. manda un rating (g, s, q, e o g,s) o toca un boton rapido\n"
            "6. manda un order (updated, score, favcount, comment, rank, id, id_desc) o toca un boton rapido\n"
            "7. el bot trae hasta 100 posts y los envia por este chat\n"
            "En age, rating y order hay un boton '< Volver' para corregir el paso anterior sin reiniciar todo.\n"
            "Comandos: /cancel cancela el flujo pendiente y /stop corta una tirada ya iniciada.\n"
            "Podes usar /help y /status en cualquier momento, incluso a mitad de un paso."
        ),
    )


@bot.message_handler(commands=["fetch"])
def handle_fetch(message):
    if not is_allowed_user(message):
        return

    prompt_for_tag(message.chat.id)


@bot.message_handler(commands=["cancel"])
def handle_cancel(message):
    if not is_allowed_user(message):
        return

    handle_in_flow_command(message)


@bot.message_handler(commands=["stop"])
def handle_stop(message):
    if not is_allowed_user(message):
        return

    current_fetch = get_active_fetch()

    if current_fetch.get("chat_id") != message.chat.id or not current_fetch.get("stop_event"):
        send_text(message.chat.id, "No hay una tirada en curso para parar.")
        return

    current_fetch["stop_event"].set()
    send_text(message.chat.id, "Parando la tirada actual. Corto en cuanto termine el post que este procesando.")


@bot.message_handler(commands=["status"])
def handle_status(message):
    if not is_allowed_user(message):
        return

    session = get_session(message.chat.id)
    current_fetch = get_active_fetch()

    if not session and current_fetch.get("chat_id") != message.chat.id:
        send_text(message.chat.id, "No hay una busqueda en curso. Usa /fetch para empezar.")
        return

    parts = []

    if session:
        parts.append(
            (
                f"Paso actual: {session.get('step', 'sin paso')}\n"
                f"tag: {session.get('tag') or '-'}\n"
                f"age: {session.get('age') or '-'}\n"
                f"rating: {session.get('rating') or '-'}\n"
                f"order: {session.get('order') or '-'}"
            )
        )

    if current_fetch.get("chat_id") == message.chat.id:
        parts.append(
            (
                "Tirada activa:\n"
                f"estado: {current_fetch.get('stage') or 'desconocido'}\n"
                f"query: {current_fetch.get('query') or '-'}"
            )
        )

    send_text(message.chat.id, "\n\n".join(parts))


@bot.message_handler(content_types=["text"])
def handle_text_fallback(message):
    if not is_allowed_user(message):
        return

    text = (message.text or "").strip()

    if text.startswith("/"):
        return

    send_text(message.chat.id, "Usa /fetch para empezar una busqueda.")


def describe_session_for_resume(session):
    parts = [f"paso actual: {session.get('step') or 'desconocido'}"]

    for field in ("tag", "age", "rating"):
        if session.get(field):
            parts.append(f"{field}: {session[field]}")

    return "\n".join(parts)


def restore_persisted_session():
    """Re-arm an in-progress /fetch wizard that a bot restart would otherwise kill silently.

    Only the configuration wizard (tag/age/rating/order) is restored — an
    active fetch already in flight when the process died is not resumable,
    since its post list and progress live only in the worker thread.
    """

    try:
        record = read_json_file(SESSION_STATE_FILE, default={}, expected_type=dict)
    except JsonStorageError as error:
        log(f"Could not read persisted session ({type(error).__name__}). Ignoring.")
        return

    chat_id = record.get("chat_id")
    session = record.get("session")

    if not chat_id or not isinstance(session, dict) or not session.get("step"):
        return

    step_handlers = {
        "tag": handle_tag_step,
        "tag_form": handle_tag_step,
        "age": handle_age_step,
        "rating": handle_rating_step,
        "order": handle_order_step,
    }
    handler = step_handlers.get(session.get("step"))

    if not handler:
        return

    with session_lock:
        chat_sessions[chat_id] = session

    bot.register_next_step_handler_by_chat_id(chat_id, handler)
    log(f"Restored interrupted session for chat {chat_id} at step {session.get('step')}.")
    send_text(
        chat_id,
        (
            "Me reinicie y encontre una busqueda sin terminar:\n"
            f"{describe_session_for_resume(session)}\n"
            "Podes seguir donde quedaste (los botones viejos, si los hay, siguen sirviendo) "
            "o mandar /fetch para empezar de nuevo."
        ),
    )


def fetch_bot_username():
    global bot_username

    try:
        bot_username = bot.get_me().username or ""
    except Exception as error:
        log(f"Could not fetch bot username ({type(error).__name__}: {error}). Inline hint will stay generic.")


def run_bot():
    if not API_TOKEN:
        raise RuntimeError("Set IMAGEBOARD_SEARCH_TELEGRAM_TOKEN before running imageboard_search_bot.py")

    log(f"Starting {BOT_NAME} {BOT_VERSION}")
    fetch_bot_username()
    restore_persisted_session()
    retry_delay = 5

    while True:
        try:
            bot.polling(non_stop=False, timeout=20, long_polling_timeout=20)
            retry_delay = 5
        except Exception as error:
            log(f"Polling error ({type(error).__name__}). Retrying in {retry_delay} seconds.")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)


if __name__ == "__main__":
    run_bot()
