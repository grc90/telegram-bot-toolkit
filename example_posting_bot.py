"""Example channel posting bot — one channel, one file.

This is the whole per-channel entry point. Everything below the config block
(the queue engine, scheduling, rate limiting, dedup, dry-run, pause/resume,
backup retention, and every Telegram command) lives in `posting_bot_shared.py`
and arrives via `create_posting_bot()`.

Adding a second channel means copying this file, changing `bot_name` and the
env var prefix, and nothing else. The `bot_name` is the stable internal
identifier: it drives the queue/settings/recent-post JSON file names and the
log file name, so it is independent of what this `.py` file is called.

The handlers at the bottom show the escape hatch — a channel can register extra
commands of its own on `APP.bot` without forking the shared engine.
"""

import argparse
import os

from config_env import get_required_env

from posting_bot_shared import create_posting_bot, load_posts_per_batch_setting, posts_per_batch_mode_hint


SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "examplechannel_settings.json")


APP = create_posting_bot(
    api_token=get_required_env("EXAMPLECHANNEL_TELEGRAM_TOKEN"),
    channel_id=get_required_env("EXAMPLECHANNEL_CHANNEL_ID"),
    bot_name="examplechannel",
    display_name="ExampleChannel Bot (@example_channel_bot)",
    timezone_env="EXAMPLECHANNEL_BOT_TIMEZONE",
    scheduled_post_hours=list(range(24)),
    posts_per_batch=load_posts_per_batch_setting(SETTINGS_FILE, 3),
    batch_post_delay_seconds=2,
    min_post_age_seconds=0,
    random_backlog=True,
    media_group_size=5,
    media_group_timeout_seconds=2.5,
    default_source_url="https://t.me/example_channel",
    extra_caption_links=[
        ("Bot", "https://t.me/example_channel_bot"),
        ("More", "https://t.me/example_channel"),
    ],
    post_queue_file=os.path.join(os.path.dirname(__file__), "examplechannel_queue.json"),
    settings_file=SETTINGS_FILE,
    allowed_user_id=get_required_env("BOTS_ALLOWED_USER_ID"),
    recent_post_window_days=28,
    max_post_messages_per_minute=20,
    rate_limit_window_seconds=60,
    wait_poll_interval_seconds=0.25,
    api_failure_cooldown=300,
)

bot = APP.bot
run_bot = APP.run_bot


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the ExampleChannel posting bot.")
    parser.add_argument("--dry-run", action="store_true", help="Preview the next batch without sending or writing state.")
    parser.add_argument("--dry-run-count", type=int, help="Override the dry-run batch size.")
    args = parser.parse_args(argv)

    if args.dry_run:
        APP.dry_run_publish_batch(max_posts=args.dry_run_count)
        return 0

    run_bot()
    return 0


def prompt_new_posts_per_batch(chat_id):
    sent_message = APP.call_telegram(
        "posts_per_batch_prompt_ack",
        bot.send_message,
        chat_id,
        (
            f"posts_per_batch actual: {APP.posts_per_batch_setting_label()}.\n"
            "Decime el nuevo valor que queres usar (numero entero mayor que 0, auto o auto2)."
        ),
    )

    if sent_message:
        bot.register_next_step_handler(sent_message, handle_posts_per_batch_reply)


def handle_posts_per_batch_reply(message):
    if not APP.is_allowed_user(message):
        return

    raw_value = (message.text or "").strip().lower()

    if raw_value in {"auto", "auto2"}:
        APP.set_posts_per_batch(raw_value)
        resolved_posts = APP.resolve_posts_per_batch()
        APP.call_telegram(
            "posts_per_batch_set_ack",
            bot.send_message,
            message.chat.id,
            (
                f"Listo. posts_per_batch ahora es {raw_value} "
                f"({posts_per_batch_mode_hint(raw_value)} -> {resolved_posts} con la queue actual)."
            ),
        )
        return

    try:
        requested_posts = int(raw_value)
    except ValueError:
        APP.call_telegram(
            "posts_per_batch_reply_invalid_ack",
            bot.send_message,
            message.chat.id,
            "No entendi el valor. Usa /posts_per_batch y responde con un entero mayor que 0, auto o auto2.",
        )
        return

    if requested_posts <= 0:
        APP.call_telegram(
            "posts_per_batch_reply_invalid_range_ack",
            bot.send_message,
            message.chat.id,
            "El numero debe ser mayor que 0.",
        )
        return

    APP.set_posts_per_batch(requested_posts)
    APP.call_telegram(
        "posts_per_batch_set_ack",
        bot.send_message,
        message.chat.id,
        f"Listo. posts_per_batch ahora es {requested_posts}.",
    )


@bot.message_handler(commands=["posts_per_batch"])
def handle_posts_per_batch(message):
    if not APP.is_allowed_user(message):
        return

    parts = (message.text or "").split(maxsplit=1)

    if len(parts) > 1:
        APP.call_telegram(
            "posts_per_batch_invalid_ack",
            bot.send_message,
            message.chat.id,
            "Usa /posts_per_batch y responde despues con un numero mayor que 0, auto o auto2.",
        )
        return

    prompt_new_posts_per_batch(message.chat.id)


@bot.message_handler(commands=["posts_per_batch_auto"])
def handle_posts_per_batch_auto(message):
    if not APP.is_allowed_user(message):
        return

    APP.set_posts_per_batch("auto")
    resolved_posts = APP.resolve_posts_per_batch()
    APP.call_telegram(
        "posts_per_batch_set_ack",
        bot.send_message,
        message.chat.id,
        (
            f"Listo. posts_per_batch ahora es auto "
            f"({posts_per_batch_mode_hint('auto')} -> {resolved_posts} con la queue actual)."
        ),
    )


@bot.message_handler(commands=["posts_per_batch_auto2"])
def handle_posts_per_batch_auto2(message):
    if not APP.is_allowed_user(message):
        return

    APP.set_posts_per_batch("auto2")
    resolved_posts = APP.resolve_posts_per_batch()
    APP.call_telegram(
        "posts_per_batch_set_ack",
        bot.send_message,
        message.chat.id,
        (
            f"Listo. posts_per_batch ahora es auto2 "
            f"({posts_per_batch_mode_hint('auto2')} -> {resolved_posts} con la queue actual)."
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
