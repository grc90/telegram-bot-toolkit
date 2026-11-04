"""Tests for dangbot state machine: load, bootstrap, check, mark_handled."""

import json

from imageboard_notifier_bot import APP as dangbot
import danbooru_notifier_shared as shared


# ---------------------------------------------------------------------------
# State load helpers
# ---------------------------------------------------------------------------

def test_load_state_returns_defaults_for_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    state = dangbot.get_state()
    assert state["bootstrapped"] is False
    assert state["handled_post_ids"] == []
    assert state["subscriber_chat_ids"] == []
    assert state["last_check_at"] is None


def test_load_state_handles_corrupt_json(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(dangbot, "state_file", str(state_file))
    alerts = []
    monkeypatch.setattr(dangbot, "send_operator_alert", lambda message: alerts.append(message))

    state = dangbot.get_state()

    assert state["bootstrapped"] is False
    # No backup available in this scenario: the operator must still be alerted.
    assert len(alerts) == 1
    assert "corrupto" in alerts[0].lower()


# ---------------------------------------------------------------------------
# State corruption: restore from backup, alert the operator
# ---------------------------------------------------------------------------

def test_load_state_restores_from_latest_backup(monkeypatch, tmp_path):
    from json_storage import write_json_atomic

    state_file = tmp_path / "state.json"
    state_file.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(dangbot, "state_file", str(state_file))

    backups_root = tmp_path / "backups"
    older_backup = backups_root / "state_20260101_000000"
    newer_backup = backups_root / "state_20260102_000000"
    older_backup.mkdir(parents=True)
    newer_backup.mkdir(parents=True)
    write_json_atomic(older_backup / "state.json", shared.default_state())
    good_state = shared.default_state()
    good_state["subscriber_chat_ids"] = [dangbot.allowed_user_id]
    good_state["bootstrapped"] = True
    write_json_atomic(newer_backup / "state.json", good_state)

    alerts = []
    monkeypatch.setattr(dangbot, "send_operator_alert", lambda message: alerts.append(message))

    state = dangbot.get_state()

    assert state["bootstrapped"] is True
    assert state["subscriber_chat_ids"] == [dangbot.allowed_user_id]
    assert len(alerts) == 1
    assert "state_20260102_000000" in alerts[0]
    # The corrupted file on disk must have been repaired with the restored state.
    assert json.loads(state_file.read_text(encoding="utf-8"))["bootstrapped"] is True


def test_load_state_alerts_when_backup_also_fails(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(dangbot, "state_file", str(state_file))

    backups_root = tmp_path / "backups"
    broken_backup = backups_root / "state_20260101_000000"
    broken_backup.mkdir(parents=True)
    (broken_backup / "state.json").write_text("{also bad", encoding="utf-8")

    alerts = []
    monkeypatch.setattr(dangbot, "send_operator_alert", lambda message: alerts.append(message))

    state = dangbot.get_state()

    assert state["bootstrapped"] is False
    assert len(alerts) == 1
    assert "backup fallido" in alerts[0]


def test_load_state_normalizes_duplicate_ids(monkeypatch, tmp_path):
    from json_storage import write_json_atomic
    state_file = str(tmp_path / "state.json")
    write_json_atomic(state_file, {
        "bootstrapped": True,
        "filter_signature": "sig",
        "handled_post_ids": ["1", "2", "1", "3"],
        "subscriber_chat_ids": [],
        "last_check_at": None,
    })
    monkeypatch.setattr(dangbot, "state_file", state_file)
    state = dangbot.get_state()
    assert state["handled_post_ids"].count("1") == 1
    assert len(state["handled_post_ids"]) == 3


# ---------------------------------------------------------------------------
# mark_handled
# ---------------------------------------------------------------------------

def test_mark_handled_adds_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    dangbot.mark_handled(["10", "20"])
    state = dangbot.get_state()
    assert "10" in state["handled_post_ids"]
    assert "20" in state["handled_post_ids"]


def test_mark_handled_deduplicates_within_call(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    dangbot.mark_handled(["1", "2", "1"])
    state = dangbot.get_state()
    assert state["handled_post_ids"].count("1") == 1


def test_mark_handled_does_not_duplicate_on_second_call(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    dangbot.mark_handled(["1"])
    dangbot.mark_handled(["1", "2"])
    state = dangbot.get_state()
    assert state["handled_post_ids"].count("1") == 1
    assert "2" in state["handled_post_ids"]


# ---------------------------------------------------------------------------
# check_danbooru_once — bootstrap behaviour
# ---------------------------------------------------------------------------

def _sample_posts():
    return [
        {"id": 1, "rating": "g", "score": 15, "file_url": "https://x.test/1.jpg", "tag_string": ""},
        {"id": 2, "rating": "g", "score": 20, "file_url": "https://x.test/2.jpg", "tag_string": ""},
    ]


def test_bootstrap_marks_posts_without_sending(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    send_calls = []
    monkeypatch.setattr(dangbot, "fetch_matching_posts", lambda **kwargs: _sample_posts())
    monkeypatch.setattr(dangbot, "send_post_to_subscribers", lambda p: send_calls.append(p["id"]) or shared.SEND_RESULT_SENT)

    result = dangbot.check_danbooru_once("test-bootstrap")

    assert result == 0
    assert send_calls == []
    state = dangbot.get_state()
    assert state["bootstrapped"] is True
    assert "1" in state["handled_post_ids"]
    assert "2" in state["handled_post_ids"]


def test_second_check_sends_only_new_posts(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))

    # Bootstrap with post 1
    monkeypatch.setattr(dangbot, "fetch_matching_posts", lambda **kwargs: [_sample_posts()[0]])
    monkeypatch.setattr(dangbot, "send_post_to_subscribers", lambda p: shared.SEND_RESULT_SENT)
    dangbot.check_danbooru_once("bootstrap")

    # Second check: post 1 known, post 2 is new
    sent_ids = []
    monkeypatch.setattr(dangbot, "fetch_matching_posts", lambda **kwargs: _sample_posts())
    monkeypatch.setattr(dangbot, "send_post_to_subscribers", lambda p: sent_ids.append(p["id"]) or shared.SEND_RESULT_SENT)
    result = dangbot.check_danbooru_once("second")

    assert result == 1
    assert 2 in sent_ids
    assert 1 not in sent_ids


def test_filter_signature_change_triggers_rebootstrap(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    posts = _sample_posts()

    monkeypatch.setattr(dangbot, "fetch_matching_posts", lambda **kwargs: posts)
    monkeypatch.setattr(dangbot, "send_post_to_subscribers", lambda p: shared.SEND_RESULT_SENT)
    dangbot.check_danbooru_once("bootstrap")

    # Simulate blacklist change
    monkeypatch.setattr(dangbot, "filter_signature", lambda: "new_signature_after_blacklist_change")
    sent_ids = []
    monkeypatch.setattr(dangbot, "send_post_to_subscribers", lambda p: sent_ids.append(p["id"]) or shared.SEND_RESULT_SENT)
    result = dangbot.check_danbooru_once("after-change")

    assert result == 0  # re-bootstrap, no sends
    assert sent_ids == []
    state = dangbot.get_state()
    assert state["filter_signature"] == "new_signature_after_blacklist_change"


# ---------------------------------------------------------------------------
# /status surfaces API cooldown state
# ---------------------------------------------------------------------------

def test_dangbot_status_shows_cooldown_active(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(dangbot, "sending_is_paused", lambda: True)
    sent = []
    monkeypatch.setattr(dangbot, "call_telegram", lambda context, func, chat_id, text: sent.append(text))

    class FakeMessage:
        text = "/status"

        class chat:
            id = 1

        class from_user:
            id = dangbot.allowed_user_id

    dangbot.handle_status(FakeMessage())

    assert sent
    assert "Cooldown API activo: si" in sent[0]


def test_dangbot_status_shows_cooldown_inactive(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(dangbot, "sending_is_paused", lambda: False)
    sent = []
    monkeypatch.setattr(dangbot, "call_telegram", lambda context, func, chat_id, text: sent.append(text))

    class FakeMessage:
        text = "/status"

        class chat:
            id = 1

        class from_user:
            id = dangbot.allowed_user_id

    dangbot.handle_status(FakeMessage())

    assert sent
    assert "Cooldown API activo: no" in sent[0]


def _fake_message(bot_module):
    class FakeMessage:
        text = "/stop"

        class chat:
            id = 1

        class from_user:
            id = bot_module.allowed_user_id

    return FakeMessage()


def test_dangbot_stop_does_not_unsubscribe(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    dangbot.update_state(lambda state: state["subscriber_chat_ids"].append("1"))
    sent = []
    monkeypatch.setattr(dangbot, "call_telegram", lambda context, func, chat_id, text: sent.append(text))

    dangbot.handle_stop(_fake_message(dangbot))

    assert "1" in dangbot.get_state()["subscriber_chat_ids"]
    assert "no tiene ninguna tirada" in sent[0]


def test_dangbot_unsubscribe_still_unsubscribes(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    dangbot.update_state(lambda state: state["subscriber_chat_ids"].append("1"))
    sent = []
    monkeypatch.setattr(dangbot, "call_telegram", lambda context, func, chat_id, text: sent.append(text))

    dangbot.handle_unsubscribe(_fake_message(dangbot))

    assert "1" not in dangbot.get_state()["subscriber_chat_ids"]
    assert "Desuscripto" in sent[0]


# ---------------------------------------------------------------------------
# Shared engine: filters and texts come from the notifier's config
# ---------------------------------------------------------------------------

def test_query_is_built_from_config():
    assert dangbot.tag_query == (
        f"rating:g score:>={dangbot.score_threshold} age:<={dangbot.max_post_age_days}d order:updated"
    )


def test_help_and_status_use_the_configured_score_band(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    sent = []
    monkeypatch.setattr(dangbot, "call_telegram", lambda context, func, chat_id, text, **kwargs: sent.append(text))

    dangbot.handle_help(_fake_message(dangbot))
    dangbot.handle_status(_fake_message(dangbot))

    assert "score 5-10" in sent[0]
    assert "score 5-10" in sent[1]


def test_all_commands_are_registered():
    registered = {
        command
        for handler in dangbot.bot.message_handlers
        for command in (handler["filters"].get("commands") or [])
    }

    assert {"start", "subscribe", "unsubscribe", "stop", "check", "test", "last6", "g6", "top", "hot",
            "help", "blacklist", "status"} <= registered

