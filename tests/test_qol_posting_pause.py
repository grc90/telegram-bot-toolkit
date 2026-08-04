"""Tests for posting bot QOL: /pause /resume, dry-run stdout, startup log."""

import json
from unittest.mock import Mock

import posting_bot_shared


# ---------------------------------------------------------------------------
# pause / resume state
# ---------------------------------------------------------------------------

def test_initially_not_paused(make_posting_app):
    app = make_posting_app()
    assert not app.is_manually_paused()


def test_pause_sets_flag(make_posting_app):
    app = make_posting_app()
    app.pause_publishing()
    assert app.is_manually_paused()


def test_resume_clears_flag(make_posting_app):
    app = make_posting_app()
    app.pause_publishing()
    app.resume_publishing()
    assert not app.is_manually_paused()


# ---------------------------------------------------------------------------
# pause prevents publish_batch
# ---------------------------------------------------------------------------

def test_pause_prevents_publish_batch(no_recent_history, monkeypatch, make_posting_app, tmp_path):
    monkeypatch.setattr(posting_bot_shared, "record_published_post", lambda *a, **k: None)

    app = make_posting_app()

    post = {"type": "photo", "file_id": "abc", "source_url": "https://example.test/1",
            "caption": "[Source](https://example.test/1)", "created_at": 1.0}
    (tmp_path / "queue.json").write_text(json.dumps([post]), encoding="utf-8")
    app.bot.send_photo = Mock(return_value=Mock())

    app.pause_publishing()
    count = app.publish_batch("test", max_posts=1)

    assert count == 0
    app.bot.send_photo.assert_not_called()


def test_resume_allows_publish_batch(no_recent_history, monkeypatch, make_posting_app, tmp_path):
    monkeypatch.setattr(posting_bot_shared, "record_published_post", lambda *a, **k: None)

    app = make_posting_app()

    post = {"type": "photo", "file_id": "abc", "source_url": "https://example.test/1",
            "caption": "[Source](https://example.test/1)", "created_at": 1.0}
    (tmp_path / "queue.json").write_text(json.dumps([post]), encoding="utf-8")
    app.bot.send_photo = Mock(return_value=Mock())

    app.pause_publishing()
    app.resume_publishing()
    count = app.publish_batch("test", max_posts=1)

    assert count == 1
    app.bot.send_photo.assert_called_once()


# ---------------------------------------------------------------------------
# dry-run stdout message
# ---------------------------------------------------------------------------

def test_dry_run_prints_to_stdout(no_recent_history, capsys, make_posting_app, tmp_path):
    app = make_posting_app()

    post = {"type": "photo", "file_id": "abc", "source_url": "https://example.test/1",
            "caption": "[Source](https://example.test/1)", "created_at": 1.0}
    (tmp_path / "queue.json").write_text(json.dumps([post]), encoding="utf-8")

    app.dry_run_publish_batch(max_posts=1)
    out = capsys.readouterr().out
    assert "DRY RUN" in out


def test_dry_run_stdout_includes_bot_name(no_recent_history, capsys, make_posting_app):
    app = make_posting_app()

    app.dry_run_publish_batch()
    out = capsys.readouterr().out
    assert app.bot_name in out


# ---------------------------------------------------------------------------
# build_status_message includes pause state
# ---------------------------------------------------------------------------

def test_status_message_shows_paused(no_recent_history, make_posting_app):
    app = make_posting_app()
    app.pause_publishing()
    status = app.build_status_message()
    assert "SI" in status or "pausa" in status.lower()


def test_status_message_shows_not_paused(no_recent_history, make_posting_app):
    app = make_posting_app()
    status = app.build_status_message()
    assert "Pausa manual: no" in status
