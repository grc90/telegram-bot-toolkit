"""Session-level environment defaults and shared fixtures for the test suite.

Loaded by pytest before any test module is imported, so module-level bot
imports (imageboard_notifier_bot, imageboard_search_bot, etc.) see
these values without each test file having to set them independently.
"""

import os
import uuid

import pytest

_TEST_TOKEN = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ123456789"

os.environ.setdefault("BOTS_ALLOWED_USER_ID", "1")
os.environ.setdefault("IMAGEBOARD_NOTIFIER_TELEGRAM_TOKEN", _TEST_TOKEN)
os.environ.setdefault("IMAGEBOARD_SEARCH_TELEGRAM_TOKEN", _TEST_TOKEN)
os.environ.setdefault("EXAMPLECHANNEL_TELEGRAM_TOKEN", _TEST_TOKEN)
os.environ.setdefault("EXAMPLECHANNEL_CHANNEL_ID", "@test")


@pytest.fixture
def make_posting_app(tmp_path):
    """Factory fixture for a throwaway PostingBotApp backed by tmp_path.

    Consolidates the ~20-line create_posting_bot boilerplate that used to be
    duplicated (with slightly different literal defaults) across
    test_posting_shared.py, test_qol_posting_pause.py, and
    test_posting_dry_run.py. Callers pass only the overrides that matter for
    their test; queue_file/settings_file always live under tmp_path.
    """
    import posting_bot_shared

    def _make(**overrides):
        queue_file = tmp_path / "queue.json"
        settings_file = tmp_path / "settings.json"

        if not queue_file.exists():
            queue_file.write_text("[]", encoding="utf-8")

        config = dict(
            api_token=_TEST_TOKEN,
            channel_id="@test",
            bot_name=f"test_{uuid.uuid4().hex[:8]}",
            timezone_env="TEST_TZ",
            scheduled_post_hours=[0],
            posts_per_batch=1,
            batch_post_delay_seconds=0,
            min_post_age_seconds=0,
            random_backlog=False,
            media_group_size=10,
            media_group_timeout_seconds=0,
            default_source_url="https://example.test",
            extra_caption_links=[],
            post_queue_file=str(queue_file),
            settings_file=str(settings_file),
            allowed_user_id="1",
            recent_post_window_days=28,
            max_post_messages_per_minute=20,
            rate_limit_window_seconds=60,
            wait_poll_interval_seconds=0.01,
            api_failure_cooldown=1,
        )
        config.update(overrides)
        return posting_bot_shared.create_posting_bot(**config)

    return _make


@pytest.fixture
def no_recent_history(monkeypatch):
    """Patch posting_bot_shared so no post is ever treated as a recent duplicate.

    Shared replacement for the was_posted_recently/filter_recent_posts monkeypatch
    pair that used to be re-typed inline in nearly every test in
    test_posting_shared.py and test_qol_posting_pause.py.
    """
    import posting_bot_shared

    monkeypatch.setattr(posting_bot_shared, "was_posted_recently", lambda *a, **k: False)
    monkeypatch.setattr(posting_bot_shared, "filter_recent_posts", lambda bot, posts, **k: posts)
