"""Tests for dev.py CLI: health, queue, filter-test, check-env, recent, stats."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dev


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

def test_health_exits_0_with_no_runtime_files(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")  # satisfy env check
    result = dev.main(["health"])
    # Missing runtime files are warnings, not errors — exit 0
    assert result == 0


def test_health_exits_0_with_valid_json_files(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("KEY=value\n", encoding="utf-8")
    for name in list(dev.RUNTIME_FILES)[:3]:
        (tmp_path / name).write_text("[]", encoding="utf-8")
    result = dev.main(["health"])
    assert result == 0


def test_health_exits_1_with_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    (tmp_path / "examplechannel_queue.json").write_text("{bad json", encoding="utf-8")
    result = dev.main(["health"])
    assert result == 1


def test_health_warns_about_leftover_tmp_files_without_failing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    leftover = tmp_path / "examplechannel_queue.json.abc123.tmp"
    leftover.write_text("{}", encoding="utf-8")

    result = dev.main(["health"])

    assert result == 0
    assert "examplechannel_queue.json.abc123.tmp" in capsys.readouterr().out
    assert leftover.exists()  # dev.py es solo lectura: avisa, no borra


def test_health_missing_env_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    # No .env file
    result = dev.main(["health"])
    assert result == 1


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------

def test_queue_unknown_bot_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "QUEUE_FILES", {})
    result = dev.main(["queue", "nonexistent"])
    assert result == 1


def test_queue_shows_count(tmp_path, monkeypatch, capsys):
    queue_file = tmp_path / "examplechannel_queue.json"
    posts = [{"type": "photo", "file_id": f"id{i}", "created_at": 1.0} for i in range(5)]
    queue_file.write_text(json.dumps(posts), encoding="utf-8")
    monkeypatch.setattr(dev, "QUEUE_FILES", {"examplechannel": queue_file})
    dev.main(["queue", "examplechannel", "--count"])
    out = capsys.readouterr().out
    assert "5" in out


def test_queue_preview_shows_n_items(tmp_path, monkeypatch, capsys):
    queue_file = tmp_path / "examplechannel_queue.json"
    posts = [{"type": "photo", "file_id": f"id{i}", "source_url": f"https://example.test/{i}", "created_at": 1.0} for i in range(10)]
    queue_file.write_text(json.dumps(posts), encoding="utf-8")
    monkeypatch.setattr(dev, "QUEUE_FILES", {"examplechannel": queue_file})
    dev.main(["queue", "examplechannel", "--preview", "3"])
    out = capsys.readouterr().out
    # Should mention "and 7 more"
    assert "7 more" in out


def test_queue_search_finds_matching_item(tmp_path, monkeypatch, capsys):
    queue_file = tmp_path / "examplechannel_queue.json"
    posts = [
        {"type": "photo", "file_id": "abc", "source_url": "https://danbooru.donmai.us/posts/999", "created_at": 1.0},
        {"type": "photo", "file_id": "xyz", "source_url": "https://pixiv.net/artworks/123", "created_at": 1.0},
    ]
    queue_file.write_text(json.dumps(posts), encoding="utf-8")
    monkeypatch.setattr(dev, "QUEUE_FILES", {"examplechannel": queue_file})
    dev.main(["queue", "examplechannel", "--search", "danbooru"])
    out = capsys.readouterr().out
    assert "1 match" in out
    assert "danbooru" in out.lower()


def test_queue_not_found_returns_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dev, "QUEUE_FILES", {"examplechannel": tmp_path / "nonexistent_queue.json"})
    result = dev.main(["queue", "examplechannel"])
    assert result == 0


# ---------------------------------------------------------------------------
# filter-test
# ---------------------------------------------------------------------------

def test_filter_test_blocked_tag(capsys):
    result = dev.main(["filter-test", "imageboard_notifier", "--tag", "ai-generated"])
    assert result == 0
    out = capsys.readouterr().out
    assert "BLOCKED" in out


def test_filter_test_not_blocked_tag(capsys):
    result = dev.main(["filter-test", "imageboard_notifier", "--tag", "1girl"])
    assert result == 0
    out = capsys.readouterr().out
    assert "NOT BLOCKED" in out


def test_filter_test_wildcard_match(capsys):
    result = dev.main(["filter-test", "imageboard_notifier", "--tag", "some_reaction_(meme)"])
    assert result == 0
    out = capsys.readouterr().out
    assert "BLOCKED" in out


def test_filter_test_invalid_bot_returns_error():
    result = dev.main(["filter-test", "examplechannel", "--tag", "ai-generated"])
    assert result == 1


def test_filter_test_no_tag_returns_error():
    result = dev.main(["filter-test", "imageboard_notifier"])
    assert result == 1


# ---------------------------------------------------------------------------
# check-env
# ---------------------------------------------------------------------------

def test_check_env_all_present(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("IMAGEBOARD_NOTIFIER_TELEGRAM_TOKEN", "123456:ABCdefGHIjklMNOpqrSTUvwxYZ12345678")
    monkeypatch.setenv("BOTS_ALLOWED_USER_ID", "1")
    result = dev.main(["check-env", "imageboard_notifier"])
    assert result == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_check_env_missing_var_returns_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    # Remove IMAGEBOARD_NOTIFIER_TELEGRAM_TOKEN from env if present
    monkeypatch.delenv("IMAGEBOARD_NOTIFIER_TELEGRAM_TOKEN", raising=False)
    monkeypatch.setenv("BOTS_ALLOWED_USER_ID", "1")
    result = dev.main(["check-env", "imageboard_notifier"])
    assert result == 1
    out = capsys.readouterr().out
    assert "MISSING" in out


def test_check_env_unknown_bot_returns_error():
    result = dev.main(["check-env", "unknown_bot"])
    assert result == 1


# ---------------------------------------------------------------------------
# recent
# ---------------------------------------------------------------------------

def test_recent_not_found_returns_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dev, "RECENT_FILES", {"examplechannel": tmp_path / "nonexistent.json"})
    result = dev.main(["recent", "examplechannel"])
    assert result == 0


def test_recent_shows_entries(tmp_path, monkeypatch, capsys):
    import time
    recent_file = tmp_path / "recent_examplechannel.json"
    entries = [
        {"signature": f"sig{i}", "recorded_at": time.time() - i * 3600, "bot": "examplechannel", "post_type": "photo"}
        for i in range(5)
    ]
    recent_file.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(dev, "RECENT_FILES", {"examplechannel": recent_file})
    dev.main(["recent", "examplechannel", "--limit", "3"])
    out = capsys.readouterr().out
    assert "3 of 5" in out or "3)" in out


def test_recent_unknown_bot_returns_error():
    result = dev.main(["recent", "imageboard_notifier"])
    assert result == 1


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def test_stats_missing_log_returns_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    result = dev.main(["stats", "imageboard_notifier"])
    assert result == 0


def test_stats_counts_published_lines(tmp_path, monkeypatch, capsys):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "examplechannel.log"
    log_file.write_text(
        "[2026-01-01 12:00:00] [examplechannel] [INFO] Published scheduled photo post. Queue size: 10.\n"
        "[2026-01-01 12:01:00] [examplechannel] [INFO] Published scheduled photo post. Queue size: 9.\n"
        "[2026-01-01 12:02:00] [examplechannel] [INFO] Polling error (Timeout). Retrying in 5 seconds.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    result = dev.main(["stats", "examplechannel"])
    assert result == 0
    out = capsys.readouterr().out
    assert "2" in out  # 2 published
