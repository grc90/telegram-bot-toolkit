"""Notifier de imageboard: avisa posts nuevos que pasan sus filtros.

Solo configuracion: el motor (fetch, filtros, envio, estado, comandos) vive en
danbooru_notifier_shared. Umbrales y overrides por .env con el prefijo IMAGEBOARD_NOTIFIER_.
Para un segundo notifier (otros ratings u otros umbrales) alcanza con copiar este
archivo y cambiar bot_name, el prefijo y los valores.
"""

import os

from config_env import get_int_env, get_required_env
from danbooru_notifier_shared import create_notifier_bot


ENV_PREFIX = "IMAGEBOARD_NOTIFIER"

APP = create_notifier_bot(
    api_token=get_required_env(f"{ENV_PREFIX}_TELEGRAM_TOKEN"),
    bot_name="imageboard_notifier",
    display_name="Imageboard Notifier",
    state_file=os.path.join(os.path.dirname(__file__), "imageboard_notifier_state.json"),
    allowed_user_id=get_required_env("BOTS_ALLOWED_USER_ID"),
    allowed_ratings=["g"],
    score_threshold=get_int_env(f"{ENV_PREFIX}_SCORE_THRESHOLD", 10),
    max_post_age_days=get_int_env(f"{ENV_PREFIX}_MAX_POST_AGE_DAYS", 7),
    danbooru_pages=get_int_env(f"{ENV_PREFIX}_DANBOORU_PAGES", 10),
    poll_interval_seconds=get_int_env(f"{ENV_PREFIX}_POLL_INTERVAL_SECONDS", 600),
    max_download_bytes=get_int_env(f"{ENV_PREFIX}_MAX_DOWNLOAD_BYTES", 49 * 1024 * 1024),
    top_score_threshold=get_int_env(f"{ENV_PREFIX}_TOP_SCORE_THRESHOLD", 20),
    hot_score_threshold=get_int_env(f"{ENV_PREFIX}_HOT_SCORE_THRESHOLD", 10),
    # Banda de score de /last6 (/g6).
    score_band=(5, 10),
)

bot = APP.bot
run_bot = APP.run_bot


def main(argv=None):
    return APP.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
