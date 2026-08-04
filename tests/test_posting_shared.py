"""Tests for PostingBotApp: enqueue, dedupe, normalize, pop, publish."""

import json
from unittest.mock import Mock

import pytest

import posting_bot_shared


def _photo(source_url="https://example.test/1", file_id="abc"):
    return {
        "type": "photo",
        "file_id": file_id,
        "source_url": source_url,
        "caption": f"[Source]({source_url})",
        "created_at": 1.0,
    }


# ---------------------------------------------------------------------------
# enqueue_post
# ---------------------------------------------------------------------------

def test_enqueue_post_adds_to_queue(no_recent_history, make_posting_app, tmp_path):
    app = make_posting_app()
    status, size = app.enqueue_post(_photo())
    assert status == "added"
    assert size == 1
    queue = json.loads((tmp_path / "queue.json").read_text())
    assert len(queue) == 1


def test_enqueue_post_skips_duplicate(no_recent_history, make_posting_app, tmp_path):
    app = make_posting_app()
    post = _photo()
    app.enqueue_post(post)
    status, size = app.enqueue_post(post)
    assert status == "duplicate"
    assert size == 1
    queue = json.loads((tmp_path / "queue.json").read_text())
    assert len(queue) == 1


def test_enqueue_post_skips_recently_posted(monkeypatch, make_posting_app, tmp_path):
    monkeypatch.setattr(posting_bot_shared, "was_posted_recently", lambda *a, **k: True)
    monkeypatch.setattr(posting_bot_shared, "filter_recent_posts", lambda bot, posts, **k: posts)
    app = make_posting_app()
    status, size = app.enqueue_post(_photo())
    assert status == "recent"
    queue = json.loads((tmp_path / "queue.json").read_text())
    assert len(queue) == 0


# ---------------------------------------------------------------------------
# describe_enqueue_result / describe_special_enqueue_result copy
# ---------------------------------------------------------------------------

def test_describe_enqueue_result_added():
    assert "Encolado" in posting_bot_shared.describe_enqueue_result("added", 5)


def test_describe_enqueue_result_duplicate():
    assert "ya esta en la queue" in posting_bot_shared.describe_enqueue_result("duplicate", 5)


def test_describe_enqueue_result_recent():
    assert "hace poco" in posting_bot_shared.describe_enqueue_result("recent", 5)


def test_describe_special_enqueue_result_added():
    assert "especial" in posting_bot_shared.describe_special_enqueue_result(True, 2).lower()


def test_describe_special_enqueue_result_duplicate():
    assert "ya estaba" in posting_bot_shared.describe_special_enqueue_result(False, 2)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

def _command_handler(app, command):
    for handler in app.bot.message_handlers:
        if command in (handler["filters"].get("commands") or []):
            return handler["function"]
    raise AssertionError(f"no handler for command={command}")


def test_help_lists_all_registered_commands(make_posting_app):
    app = make_posting_app()
    app.bot.send_message = Mock()
    handler = _command_handler(app, "help")

    class FakeMessage:
        chat = Mock(id=1)
        from_user = Mock(id=1)

    handler(FakeMessage())

    app.bot.send_message.assert_called_once()
    text = app.bot.send_message.call_args[0][1]

    for command in ("post_now", "stop", "queue", "status", "special", "done", "pause", "resume", "backup", "help"):
        assert f"/{command}" in text


# ---------------------------------------------------------------------------
# handle_photo / process_media_group send an ack (the actual UX fix)
# ---------------------------------------------------------------------------

def _photo_handler(app):
    for handler in app.bot.message_handlers:
        if handler["filters"].get("content_types") == ["photo"]:
            return handler["function"]
    raise AssertionError("no photo handler registered")


class _FakePhotoSize:
    def __init__(self, file_id="abc"):
        self.file_id = file_id


class _FakePhotoMessage:
    def __init__(self, chat_id=555, user_id=1, caption=None):
        self.chat = Mock(id=chat_id)
        self.from_user = Mock(id=user_id)
        self.photo = [_FakePhotoSize()]
        self.caption = caption
        self.media_group_id = None


class _FakePhotoMessageForGroup:
    def __init__(self, message_id, chat_id):
        self.message_id = message_id
        self.chat = Mock(id=chat_id)
        self.photo = [_FakePhotoSize()]
        self.caption = None


def test_handle_photo_acks_added(no_recent_history, make_posting_app):
    app = make_posting_app()
    app.bot.send_message = Mock()
    handler = _photo_handler(app)

    handler(_FakePhotoMessage(chat_id=555))

    app.bot.send_message.assert_called_once_with(555, "Encolado. Queue: 1 post(s).")


def test_handle_photo_acks_duplicate(no_recent_history, make_posting_app):
    app = make_posting_app()
    app.bot.send_message = Mock()
    handler = _photo_handler(app)

    handler(_FakePhotoMessage(chat_id=555))
    handler(_FakePhotoMessage(chat_id=555))

    assert app.bot.send_message.call_count == 2
    last_call_args = app.bot.send_message.call_args
    assert last_call_args[0] == (555, "Ese post ya esta en la queue.")


def test_handle_photo_acks_special_mode(no_recent_history, make_posting_app):
    app = make_posting_app()
    app.bot.send_message = Mock()
    app.set_special_mode(True)
    handler = _photo_handler(app)

    handler(_FakePhotoMessage(chat_id=555))

    app.bot.send_message.assert_called_once_with(555, "Encolado en modo especial. Especiales: 1 post(s).")


def test_process_media_group_acks_added(no_recent_history, make_posting_app):
    app = make_posting_app()
    app.bot.send_message = Mock()

    media_group_id = "grp1"
    with app.media_group_lock:
        app.media_groups[media_group_id] = [
            _FakePhotoMessageForGroup(message_id=1, chat_id=777),
            _FakePhotoMessageForGroup(message_id=2, chat_id=777),
        ]
        app.media_group_special_modes[media_group_id] = False

    app.process_media_group(media_group_id)

    app.bot.send_message.assert_called_once()
    call_args = app.bot.send_message.call_args[0]
    assert call_args[0] == 777
    assert "Encolado" in call_args[1]


# ---------------------------------------------------------------------------
# normalize_queue
# ---------------------------------------------------------------------------

def test_normalize_queue_removes_malformed(monkeypatch, make_posting_app):
    monkeypatch.setattr(posting_bot_shared, "filter_recent_posts", lambda bot, posts, **k: posts)
    app = make_posting_app()
    valid = _photo()
    malformed_no_file_id = {"type": "photo"}
    malformed_not_dict = "bad"
    result, dupes, recent, bad = app.normalize_queue([valid, malformed_no_file_id, malformed_not_dict])
    assert len(result) == 1
    assert bad == 2


def test_normalize_queue_deduplicates(monkeypatch, make_posting_app):
    monkeypatch.setattr(posting_bot_shared, "filter_recent_posts", lambda bot, posts, **k: posts)
    app = make_posting_app()
    post = _photo()
    result, dupes, recent, bad = app.normalize_queue([post, post])
    assert len(result) == 1
    assert dupes == 1


# ---------------------------------------------------------------------------
# pop_next_post
# ---------------------------------------------------------------------------

def test_pop_next_post_removes_item(no_recent_history, make_posting_app, tmp_path):
    app = make_posting_app()
    post = _photo()
    (tmp_path / "queue.json").write_text(json.dumps([post]), encoding="utf-8")
    popped, remaining = app.pop_next_post()
    assert popped is not None
    assert remaining == 0
    queue = json.loads((tmp_path / "queue.json").read_text())
    assert len(queue) == 0


def test_pop_next_post_returns_none_on_empty_queue(no_recent_history, make_posting_app):
    app = make_posting_app()
    popped, remaining = app.pop_next_post()
    assert popped is None
    assert remaining == 0


# ---------------------------------------------------------------------------
# publish_batch
# ---------------------------------------------------------------------------

def test_publish_batch_sends_post_and_returns_count(no_recent_history, monkeypatch, make_posting_app, tmp_path):
    monkeypatch.setattr(posting_bot_shared, "record_published_post", lambda *a, **k: None)
    app = make_posting_app(posts_per_batch=1)
    post = _photo()
    (tmp_path / "queue.json").write_text(json.dumps([post]), encoding="utf-8")
    app.bot.send_photo = Mock(return_value=Mock())
    count = app.publish_batch("test", max_posts=1)
    assert count == 1
    app.bot.send_photo.assert_called_once()


def test_publish_batch_does_not_send_when_queue_empty(no_recent_history, monkeypatch, make_posting_app):
    monkeypatch.setattr(posting_bot_shared, "record_published_post", lambda *a, **k: None)
    app = make_posting_app()
    app.bot.send_photo = Mock()
    count = app.publish_batch("test", max_posts=1)
    assert count == 0
    app.bot.send_photo.assert_not_called()


def test_publish_batch_restores_post_on_telegram_failure(no_recent_history, monkeypatch, make_posting_app, tmp_path):
    monkeypatch.setattr(posting_bot_shared, "record_published_post", lambda *a, **k: None)
    app = make_posting_app(posts_per_batch=1)
    post = _photo()
    (tmp_path / "queue.json").write_text(json.dumps([post]), encoding="utf-8")
    app.bot.send_photo = Mock(return_value=None)  # simulate failure
    count = app.publish_batch("test", max_posts=1)
    assert count == 0
    # post should be restored to queue
    queue = json.loads((tmp_path / "queue.json").read_text())
    assert len(queue) == 1


# ---------------------------------------------------------------------------
# special mode
# ---------------------------------------------------------------------------

def test_special_queue_is_separate_from_main_queue(no_recent_history, make_posting_app, tmp_path):
    app = make_posting_app()
    app.set_special_mode(True)
    post = _photo()
    added, size = app.enqueue_special_post(post)
    assert added is True
    assert size == 1
    # main queue still empty
    queue = json.loads((tmp_path / "queue.json").read_text())
    assert len(queue) == 0


def test_enqueue_special_post_skips_duplicate(make_posting_app):
    app = make_posting_app()
    post = _photo()
    app.enqueue_special_post(post)
    added, size = app.enqueue_special_post(post)
    assert added is False
    assert size == 1


# ---------------------------------------------------------------------------
# pause_sending alerts the operator
# ---------------------------------------------------------------------------

def test_pause_sending_alerts_operator(make_posting_app):
    app = make_posting_app()
    alerts = []
    app.send_operator_alert = lambda message: alerts.append(message)

    app.pause_sending("test_context", TimeoutError("boom"))

    assert len(alerts) == 1
    assert app.bot_name in alerts[0]
    assert app.sending_is_paused()


# ---------------------------------------------------------------------------
# build_queue_status_message empty-queue copy
# ---------------------------------------------------------------------------

def test_build_queue_status_message_empty_queue_has_clear_copy():
    message = posting_bot_shared.build_queue_status_message(0, 0, 0, 0, 0, False, 0)
    assert "vacia" in message.lower()


def test_build_queue_status_message_all_hidden_keeps_detailed_copy():
    message = posting_bot_shared.build_queue_status_message(0, 3, 2, 1, 0, False, 0)
    assert "vacia" not in message.lower()
    assert "3 guardados" in message
    assert "2 duplicado" in message
    assert "1 publicado" in message


def test_describe_hidden_queue_reasons_lists_each_cause():
    text = posting_bot_shared.describe_hidden_queue_reasons(2, 1, 3)
    assert "2 duplicado(s)" in text
    assert "1 publicado(s) recientemente" in text
    assert "3 invalido(s)" in text


def test_describe_hidden_queue_reasons_empty_is_zero():
    assert posting_bot_shared.describe_hidden_queue_reasons(0, 0, 0) == "0"


# ---------------------------------------------------------------------------
# posts_per_batch_mode_hint
# ---------------------------------------------------------------------------

def test_posts_per_batch_mode_hint_explains_auto():
    assert "100" in posting_bot_shared.posts_per_batch_mode_hint("auto")


def test_posts_per_batch_mode_hint_explains_auto2():
    assert "auto" in posting_bot_shared.posts_per_batch_mode_hint("auto2")


def test_posts_per_batch_mode_hint_none_for_numeric_mode():
    assert posting_bot_shared.posts_per_batch_mode_hint(5) is None


# ---------------------------------------------------------------------------
# automatic backup retention cleanup (backup_loop)
# ---------------------------------------------------------------------------

def test_run_backup_retention_cleanup_delegates_with_24h_apply(monkeypatch, make_posting_app):
    app = make_posting_app()
    calls = []
    monkeypatch.setattr(
        posting_bot_shared,
        "clean_old_backup_folders",
        lambda **kwargs: calls.append(kwargs) or {"kept": [], "to_delete": [], "failed": []},
    )

    app.run_backup_retention_cleanup()

    assert calls == [{"retention_hours": posting_bot_shared.DEFAULT_QUEUE_RETENTION_HOURS, "apply": True}]


class _StopLoop(Exception):
    """Sentinel used to escape backup_loop's `while True` after one iteration."""


def test_backup_loop_runs_retention_cleanup_after_backup(monkeypatch, make_posting_app):
    app = make_posting_app()
    monkeypatch.setattr(posting_bot_shared.time, "sleep", Mock(side_effect=[None, _StopLoop()]))
    monkeypatch.setattr(app, "run_backup", lambda: Mock(name="backup_dir"))
    cleanup_calls = []
    monkeypatch.setattr(
        app,
        "run_backup_retention_cleanup",
        lambda: cleanup_calls.append(1) or {"kept": [], "to_delete": [], "failed": []},
    )
    monkeypatch.setattr(app, "send_operator_alert", lambda message: None)

    with pytest.raises(_StopLoop):
        app.backup_loop()

    assert cleanup_calls == [1]


def test_backup_loop_alerts_when_cleanup_has_failures(monkeypatch, make_posting_app):
    app = make_posting_app()
    monkeypatch.setattr(posting_bot_shared.time, "sleep", Mock(side_effect=[None, _StopLoop()]))
    monkeypatch.setattr(app, "run_backup", lambda: Mock(name="backup_dir"))
    monkeypatch.setattr(
        app,
        "run_backup_retention_cleanup",
        lambda: {"kept": [], "to_delete": [], "failed": [("some_folder", "locked")]},
    )
    alerts = []
    monkeypatch.setattr(app, "send_operator_alert", lambda message: alerts.append(message))

    with pytest.raises(_StopLoop):
        app.backup_loop()

    assert any("Limpieza de backups" in message for message in alerts)


def test_backup_loop_cleanup_failure_does_not_crash_loop(monkeypatch, make_posting_app):
    app = make_posting_app()
    monkeypatch.setattr(posting_bot_shared.time, "sleep", Mock(side_effect=[None, _StopLoop()]))
    monkeypatch.setattr(app, "run_backup", lambda: Mock(name="backup_dir"))

    def _boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(app, "run_backup_retention_cleanup", _boom)
    monkeypatch.setattr(app, "send_operator_alert", lambda message: None)

    with pytest.raises(_StopLoop):
        app.backup_loop()
