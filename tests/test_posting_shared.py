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

    for command in (
        "post_now",
        "stop",
        "queue",
        "delete",
        "status",
        "special",
        "done",
        "pause",
        "resume",
        "backup",
        "help",
    ):
        assert f"/{command}" in text



# ---------------------------------------------------------------------------
# /posts_per_batch lives in the shared engine (same behavior for every channel)
# ---------------------------------------------------------------------------

class _OperatorMessage:
    def __init__(self, text):
        self.text = text
        self.chat = Mock(id=1)
        self.from_user = Mock(id=1)


def test_posts_per_batch_auto_commands_set_mode_and_persist(make_posting_app, tmp_path):
    app = make_posting_app(posts_per_batch=3)
    app.bot.send_message = Mock()

    _command_handler(app, "posts_per_batch_auto2")(_OperatorMessage("/posts_per_batch_auto2"))

    assert app.posts_per_batch == "auto2"
    assert json.loads((tmp_path / "settings.json").read_text())["posts_per_batch"] == "auto2"
    assert "auto2" in app.bot.send_message.call_args[0][1]


def test_posts_per_batch_prompt_then_numeric_reply(make_posting_app):
    app = make_posting_app(posts_per_batch=3)
    app.bot.send_message = Mock(return_value=Mock())
    captured = {}
    app.bot.register_next_step_handler = lambda message, handler: captured.setdefault("handler", handler)

    _command_handler(app, "posts_per_batch")(_OperatorMessage("/posts_per_batch"))
    captured["handler"](_OperatorMessage("7"))

    assert app.posts_per_batch == 7
    assert app.bot.send_message.call_args[0][1] == "Listo. posts_per_batch ahora es 7."


@pytest.mark.parametrize("reply", ["0", "abc"])
def test_posts_per_batch_reply_rejects_invalid_values(make_posting_app, reply):
    app = make_posting_app(posts_per_batch=3)
    app.bot.send_message = Mock(return_value=Mock())
    captured = {}
    app.bot.register_next_step_handler = lambda message, handler: captured.setdefault("handler", handler)

    _command_handler(app, "posts_per_batch")(_OperatorMessage("/posts_per_batch"))
    captured["handler"](_OperatorMessage(reply))

    assert app.posts_per_batch == 3


def test_posts_per_batch_ignores_other_users(make_posting_app):
    app = make_posting_app(posts_per_batch=3)
    app.bot.send_message = Mock()
    message = _OperatorMessage("/posts_per_batch_auto")
    message.from_user = Mock(id=999)

    _command_handler(app, "posts_per_batch_auto")(message)

    assert app.posts_per_batch == 3
    app.bot.send_message.assert_not_called()

# ---------------------------------------------------------------------------
# /post_now xN multiplies the effective batch size
# ---------------------------------------------------------------------------

def _post_now(app, text):
    app.bot.send_message = Mock()
    handler = _command_handler(app, "post_now")

    class FakeMessage:
        chat = Mock(id=1)
        from_user = Mock(id=1)

    message = FakeMessage()
    message.text = text
    handler(message)
    return app.bot.send_message.call_args[0][1]


def test_post_now_multiplier_uses_fixed_batch_size(make_posting_app):
    app = make_posting_app(posts_per_batch=3)

    text = _post_now(app, "/post_now x2")

    assert app.manual_batch_size == 6
    assert app.post_now_event.is_set()
    assert "2 tanda(s) de 3 efectivos" in text


def test_post_now_multiplier_resolves_auto_mode(make_posting_app, monkeypatch):
    app = make_posting_app(posts_per_batch="auto")
    monkeypatch.setattr(app, "queue_size", lambda: 250)

    _post_now(app, "/post_now X3")

    assert app.manual_batch_size == 9


def test_post_now_plain_number_unchanged(make_posting_app):
    app = make_posting_app(posts_per_batch=3)

    _post_now(app, "/post_now 4")

    assert app.manual_batch_size == 4


@pytest.mark.parametrize("argument", ["x0", "x-1", "xabc", "x"])
def test_post_now_multiplier_rejects_invalid(make_posting_app, argument):
    app = make_posting_app(posts_per_batch=3)

    _post_now(app, f"/post_now {argument}")

    assert app.manual_batch_size is None
    assert not app.post_now_event.is_set()


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


def test_handle_photo_stores_twitter_source_without_modifiers(no_recent_history, make_posting_app):
    app = make_posting_app()
    app.bot.send_message = Mock()
    handler = _photo_handler(app)

    handler(_FakePhotoMessage(caption="https://x.com/x3li9/status/2116335963693551890?s=20&t=PwqO8lGfYYWgrsZh5Eg"))

    queued = app.load_queue()
    assert queued[0]["source_url"] == "https://x.com/x3li9/status/2116335963693551890"
    assert "?s=20" not in queued[0]["caption"]


def test_process_media_group_stores_twitter_source_without_modifiers(no_recent_history, make_posting_app):
    app = make_posting_app()
    app.bot.send_message = Mock()

    first = _FakePhotoMessageForGroup(message_id=1, chat_id=777)
    first.caption = "https://twitter.com/x3li9/status/2116335963693551890?s=46"

    with app.media_group_lock:
        app.media_groups["grp1"] = [first, _FakePhotoMessageForGroup(message_id=2, chat_id=777)]
        app.media_group_special_modes["grp1"] = False

    app.process_media_group("grp1")

    assert app.load_queue()[0]["source_url"] == "https://twitter.com/x3li9/status/2116335963693551890"


def test_build_post_keeps_query_on_non_twitter_sources(make_posting_app):
    app = make_posting_app()

    post = app.build_post("photo", {"file_id": "abc"}, "https://gall.dcinside.com/board/view/?id=x&no=1")

    assert post["source_url"] == "https://gall.dcinside.com/board/view/?id=x&no=1"


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


def test_publish_batch_strips_twitter_modifiers_from_already_queued_post(
    no_recent_history, monkeypatch, make_posting_app, tmp_path
):
    monkeypatch.setattr(posting_bot_shared, "record_published_post", lambda *a, **k: None)
    app = make_posting_app(posts_per_batch=1)
    post = _photo(source_url="https://x.com/x3li9/status/2116335963693551890?a=PwqO8lGfYYWgrsZh5Eg&s=20")
    (tmp_path / "queue.json").write_text(json.dumps([post]), encoding="utf-8")
    app.bot.send_photo = Mock(return_value=Mock())

    assert app.publish_batch("test", max_posts=1) == 1

    caption = app.bot.send_photo.call_args.kwargs["caption"]
    assert "[Source](https://x.com/x3li9/status/2116335963693551890)" in caption


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


def _telegram_400(description):
    return posting_bot_shared.telebot.apihelper.ApiTelegramException(
        "sendPhoto", Mock(), {"error_code": 400, "description": description}
    )


def _app_with_queue(make_posting_app, tmp_path, monkeypatch, posts, **overrides):
    monkeypatch.setattr(posting_bot_shared, "record_published_post", lambda *a, **k: None)
    app = make_posting_app(**overrides)
    (tmp_path / "queue.json").write_text(json.dumps(posts), encoding="utf-8")
    alerts = []
    app.send_operator_alert = alerts.append
    return app, alerts


def test_publish_batch_drops_unsendable_post_and_keeps_going(no_recent_history, monkeypatch, make_posting_app, tmp_path):
    bad, good = _photo("https://example.test/bad", "bad"), _photo("https://example.test/good", "good")
    app, alerts = _app_with_queue(make_posting_app, tmp_path, monkeypatch, [bad, good])

    def send_photo(channel_id, file_id, **kwargs):
        if file_id == "bad":
            raise _telegram_400("Bad Request: wrong file identifier/HTTP URL specified")
        return Mock()

    app.bot.send_photo = send_photo

    assert app.publish_batch("test", max_posts=2) == 1
    assert json.loads((tmp_path / "queue.json").read_text()) == []
    assert len(alerts) == 1
    assert "https://example.test/bad" in alerts[0]


def test_publish_batch_keeps_queue_on_channel_wide_rejection(no_recent_history, monkeypatch, make_posting_app, tmp_path):
    first, second = _photo("https://example.test/1", "a"), _photo("https://example.test/2", "b")
    app, alerts = _app_with_queue(make_posting_app, tmp_path, monkeypatch, [first, second])
    app.bot.send_photo = Mock(side_effect=_telegram_400("Bad Request: chat not found"))

    assert app.publish_batch("test", max_posts=2) == 0
    # Un error de canal no puede vaciar la queue: se restaura y se corta el lote.
    assert len(json.loads((tmp_path / "queue.json").read_text())) == 2
    assert app.bot.send_photo.call_count == 1
    assert alerts == []


def test_publish_batch_drops_unsendable_media_group(no_recent_history, monkeypatch, make_posting_app, tmp_path):
    album = {
        "type": "media_group",
        "file_ids": ["x", "y"],
        "source_url": "https://example.test/album",
        "caption": "[Source](https://example.test/album)",
        "created_at": 1.0,
    }
    app, alerts = _app_with_queue(make_posting_app, tmp_path, monkeypatch, [album])
    app.bot.send_media_group = Mock(side_effect=_telegram_400("Bad Request: group send failed"))

    assert app.publish_batch("test", max_posts=1) == 0
    assert json.loads((tmp_path / "queue.json").read_text()) == []
    assert len(alerts) == 1


def test_publish_special_batch_drops_unsendable_post(no_recent_history, monkeypatch, make_posting_app, tmp_path):
    app, alerts = _app_with_queue(make_posting_app, tmp_path, monkeypatch, [])
    app.enqueue_special_post(_photo("https://example.test/bad", "bad"))
    app.enqueue_special_post(_photo("https://example.test/good", "good"))

    def send_photo(channel_id, file_id, **kwargs):
        if file_id == "bad":
            raise _telegram_400("Bad Request: PHOTO_INVALID_DIMENSIONS")
        return Mock()

    app.bot.send_photo = send_photo

    assert app.publish_special_batch("test") == 1
    assert app.special_queue_size() == 0
    assert len(alerts) == 1


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
    monkeypatch.setattr(app, "backup_was_made_recently", lambda: False)
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
    monkeypatch.setattr(app, "backup_was_made_recently", lambda: False)
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
    monkeypatch.setattr(app, "backup_was_made_recently", lambda: False)
    monkeypatch.setattr(app, "run_backup", lambda: Mock(name="backup_dir"))

    def _boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(app, "run_backup_retention_cleanup", _boom)
    monkeypatch.setattr(app, "send_operator_alert", lambda message: None)

    with pytest.raises(_StopLoop):
        app.backup_loop()



def test_backup_loop_skips_backup_another_bot_just_made_but_still_cleans(monkeypatch, make_posting_app):
    app = make_posting_app()
    monkeypatch.setattr(posting_bot_shared.time, "sleep", Mock(side_effect=[None, _StopLoop()]))
    monkeypatch.setattr(app, "backup_was_made_recently", lambda: True)
    backups, cleanups, alerts = [], [], []
    monkeypatch.setattr(app, "run_backup", lambda: backups.append(1))
    monkeypatch.setattr(
        app, "run_backup_retention_cleanup", lambda: cleanups.append(1) or {"kept": [], "to_delete": [], "failed": []}
    )
    monkeypatch.setattr(app, "send_operator_alert", alerts.append)

    with pytest.raises(_StopLoop):
        app.backup_loop()

    assert backups == []
    assert cleanups == [1]
    assert alerts == []


@pytest.mark.parametrize("age_hours, expected", [(None, False), (1, True), (2.9, True), (3.1, False), (-1, False)])
def test_backup_was_made_recently_uses_half_the_interval(monkeypatch, make_posting_app, age_hours, expected):
    app = make_posting_app()
    age = None if age_hours is None else age_hours * 3600
    monkeypatch.setattr(posting_bot_shared, "latest_backup_age_seconds", lambda: age)

    assert app.backup_was_made_recently() is expected

# ---------------------------------------------------------------------------
# /delete — remove a queued post by its source URL
# ---------------------------------------------------------------------------

def test_source_match_key_ignores_scheme_and_trailing_slash():
    key = posting_bot_shared.source_match_key("https://danbooru.donmai.us/posts/1")
    assert posting_bot_shared.source_match_key("http://danbooru.donmai.us/posts/1/") == key
    assert posting_bot_shared.source_match_key("danbooru.donmai.us/posts/1") == key
    assert posting_bot_shared.source_match_key("  https://danbooru.donmai.us/posts/1  ") == key


def test_source_match_key_keeps_distinct_posts_distinct():
    assert posting_bot_shared.source_match_key(
        "https://danbooru.donmai.us/posts/1"
    ) != posting_bot_shared.source_match_key("https://danbooru.donmai.us/posts/2")


def test_source_match_key_rejects_empty_input():
    assert posting_bot_shared.source_match_key("") is None
    assert posting_bot_shared.source_match_key("   ") is None
    assert posting_bot_shared.source_match_key(None) is None


def test_remove_queued_posts_by_source_url_removes_only_the_match(no_recent_history, make_posting_app, tmp_path):
    app = make_posting_app()
    app.enqueue_post(_photo(source_url="https://example.test/1", file_id="a"))
    app.enqueue_post(_photo(source_url="https://example.test/2", file_id="b"))

    removed_count, remaining_size = app.remove_queued_posts_by_source_url("https://example.test/1")

    assert (removed_count, remaining_size) == (1, 1)
    queue = json.loads((tmp_path / "queue.json").read_text())
    assert [post["source_url"] for post in queue] == ["https://example.test/2"]


def test_remove_queued_posts_by_source_url_accepts_url_without_scheme(no_recent_history, make_posting_app, tmp_path):
    app = make_posting_app()
    app.enqueue_post(_photo(source_url="https://example.test/1", file_id="a"))

    removed_count, remaining_size = app.remove_queued_posts_by_source_url("example.test/1/")

    assert (removed_count, remaining_size) == (1, 0)
    assert json.loads((tmp_path / "queue.json").read_text()) == []


def test_remove_queued_posts_by_source_url_matches_caption_only_post(no_recent_history, make_posting_app):
    app = make_posting_app()
    app.enqueue_post(
        {
            "type": "photo",
            "file_id": "a",
            "caption": "[Source](https://example.test/only%2Din%2Dcaption)",
            "created_at": 1.0,
        }
    )

    removed_count, remaining_size = app.remove_queued_posts_by_source_url(
        "https://example.test/only%2Din%2Dcaption"
    )

    assert (removed_count, remaining_size) == (1, 0)


def test_remove_queued_posts_by_source_url_reports_no_match(no_recent_history, make_posting_app, tmp_path):
    app = make_posting_app()
    app.enqueue_post(_photo(source_url="https://example.test/1", file_id="a"))

    removed_count, remaining_size = app.remove_queued_posts_by_source_url("https://example.test/999")

    assert (removed_count, remaining_size) == (0, 1)
    assert len(json.loads((tmp_path / "queue.json").read_text())) == 1


def test_remove_queued_posts_by_source_url_rejects_blank_url(no_recent_history, make_posting_app):
    app = make_posting_app()

    with pytest.raises(ValueError):
        app.remove_queued_posts_by_source_url("   ")


class _FakeDeleteMessage:
    def __init__(self, text, user_id=1):
        self.text = text
        self.chat = Mock(id=555)
        self.from_user = Mock(id=user_id)


def test_delete_handler_removes_post_and_acks(no_recent_history, make_posting_app, tmp_path):
    app = make_posting_app()
    app.enqueue_post(_photo(source_url="https://example.test/1", file_id="a"))
    app.bot.send_message = Mock()

    _command_handler(app, "delete")(_FakeDeleteMessage("/delete https://example.test/1"))

    assert json.loads((tmp_path / "queue.json").read_text()) == []
    assert "Borre 1 post" in app.bot.send_message.call_args[0][1]


def test_delete_handler_without_url_explains_usage(no_recent_history, make_posting_app, tmp_path):
    app = make_posting_app()
    app.enqueue_post(_photo(source_url="https://example.test/1", file_id="a"))
    app.bot.send_message = Mock()

    _command_handler(app, "delete")(_FakeDeleteMessage("/delete"))

    assert "/delete <url>" in app.bot.send_message.call_args[0][1]
    assert len(json.loads((tmp_path / "queue.json").read_text())) == 1


def test_delete_handler_ignores_other_users(no_recent_history, make_posting_app, tmp_path):
    app = make_posting_app()
    app.enqueue_post(_photo(source_url="https://example.test/1", file_id="a"))
    app.bot.send_message = Mock()

    _command_handler(app, "delete")(_FakeDeleteMessage("/delete https://example.test/1", user_id=99))

    app.bot.send_message.assert_not_called()
    assert len(json.loads((tmp_path / "queue.json").read_text())) == 1


def test_describe_delete_result_copy():
    assert "No encontre" in posting_bot_shared.describe_delete_result(0, 3)
    assert "Borre 1 post " in posting_bot_shared.describe_delete_result(1, 2)
    assert "Borre 2 posts" in posting_bot_shared.describe_delete_result(2, 2)
