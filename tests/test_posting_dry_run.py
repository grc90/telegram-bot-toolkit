import json
from datetime import datetime, timedelta
from unittest.mock import Mock


def test_dry_run_does_not_send_or_modify_queue(make_posting_app, tmp_path):
    queue_file = tmp_path / "queue.json"
    queue = [
        {
            "type": "photo",
            "file_id": "file-a",
            "caption": "[Source](https://example.test/a)",
            "source_url": "https://example.test/a",
            "created_at": 1.0,
        }
    ]
    queue_file.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    before = queue_file.read_text(encoding="utf-8")

    app = make_posting_app(
        channel_id="@dryrun",
        bot_name="dry_run_test",
        timezone_env="DRY_RUN_TEST_TIMEZONE",
        default_source_url="https://example.test/default",
    )
    app.bot.send_photo = Mock()
    app.bot.send_media_group = Mock()
    app.bot.send_message = Mock()

    assert app.dry_run_publish_batch(max_posts=1) == 1
    assert queue_file.read_text(encoding="utf-8") == before
    app.bot.send_photo.assert_not_called()
    app.bot.send_media_group.assert_not_called()
    app.bot.send_message.assert_not_called()
    assert not (queue_file.parent / "recent_posts_dry_run_test.json").exists()


def test_scheduled_batch_lateness_grace_skips_old_wakeups(make_posting_app):
    app = make_posting_app(
        channel_id="@dryrun",
        bot_name="dry_run_test_late_schedule",
        timezone_env="DRY_RUN_TEST_TIMEZONE",
        default_source_url="https://example.test/default",
        scheduled_post_lateness_grace_seconds=300,
    )

    slightly_late = datetime.now(app.schedule_timezone) - timedelta(seconds=60)
    very_late = datetime.now(app.schedule_timezone) - timedelta(seconds=600)

    assert app.scheduled_batch_is_too_late(slightly_late)[0] is False
    assert app.scheduled_batch_is_too_late(very_late)[0] is True
