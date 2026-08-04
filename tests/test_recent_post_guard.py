"""Tests for recent_post_guard: record, query, filter, pruning, and lock."""

import time

import recent_post_guard
from json_storage import write_json_atomic


def _make_post(file_id="abc", source_url="https://example.test/a"):
    return {"type": "photo", "file_id": file_id, "source_url": source_url}


def _patch_files(monkeypatch, tmp_path):
    """Redirect all recent-posts file paths to tmp_path."""
    monkeypatch.setattr(
        recent_post_guard,
        "_recent_posts_file",
        lambda name: str(tmp_path / f"recent_{name}.json"),
    )


# ---------------------------------------------------------------------------
# Basic read/write round-trip
# ---------------------------------------------------------------------------

def test_was_posted_recently_returns_false_for_new_post(monkeypatch, tmp_path):
    _patch_files(monkeypatch, tmp_path)
    post = _make_post()
    assert not recent_post_guard.was_posted_recently("bot", post)


def test_record_then_was_posted_recently_returns_true(monkeypatch, tmp_path):
    _patch_files(monkeypatch, tmp_path)
    post = _make_post()
    recent_post_guard.record_published_post("bot", post)
    assert recent_post_guard.was_posted_recently("bot", post)


def test_different_post_not_seen_after_record(monkeypatch, tmp_path):
    _patch_files(monkeypatch, tmp_path)
    post_a = _make_post("a", "https://example.test/a")
    post_b = _make_post("b", "https://example.test/b")
    recent_post_guard.record_published_post("bot", post_a)
    assert not recent_post_guard.was_posted_recently("bot", post_b)


# ---------------------------------------------------------------------------
# Pruning of expired entries
# ---------------------------------------------------------------------------

def test_expired_entry_is_not_seen(monkeypatch, tmp_path):
    _patch_files(monkeypatch, tmp_path)
    post = _make_post("x", "https://example.test/x")
    old_ts = time.time() - (29 * 24 * 60 * 60)  # 29 days ago
    posts_file = str(tmp_path / "recent_bot.json")
    sig = recent_post_guard._signature_key(post)
    write_json_atomic(posts_file, [{"signature": sig, "recorded_at": old_ts, "bot": "bot", "post_type": "photo"}])
    assert not recent_post_guard.was_posted_recently("bot", post, window_days=28)


def test_entry_within_window_is_seen(monkeypatch, tmp_path):
    _patch_files(monkeypatch, tmp_path)
    post = _make_post("y", "https://example.test/y")
    recent_ts = time.time() - (10 * 24 * 60 * 60)  # 10 days ago
    posts_file = str(tmp_path / "recent_bot.json")
    sig = recent_post_guard._signature_key(post)
    write_json_atomic(posts_file, [{"signature": sig, "recorded_at": recent_ts, "bot": "bot", "post_type": "photo"}])
    assert recent_post_guard.was_posted_recently("bot", post, window_days=28)


# ---------------------------------------------------------------------------
# filter_recent_posts
# ---------------------------------------------------------------------------

def test_filter_recent_posts_excludes_recorded(monkeypatch, tmp_path):
    _patch_files(monkeypatch, tmp_path)
    post_a = _make_post("a", "https://example.test/a")
    post_b = _make_post("b", "https://example.test/b")
    recent_post_guard.record_published_post("bot", post_a)
    result = recent_post_guard.filter_recent_posts("bot", [post_a, post_b])
    assert result == [post_b]


def test_filter_recent_posts_returns_all_when_history_empty(monkeypatch, tmp_path):
    _patch_files(monkeypatch, tmp_path)
    posts = [_make_post("a", "https://example.test/a"), _make_post("b", "https://example.test/b")]
    result = recent_post_guard.filter_recent_posts("bot", posts)
    assert result == posts


# ---------------------------------------------------------------------------
# persist_pruned=False must not write anything
# ---------------------------------------------------------------------------

def test_was_posted_recently_no_persist_does_not_create_file(monkeypatch, tmp_path):
    _patch_files(monkeypatch, tmp_path)
    post = _make_post()
    recent_post_guard.was_posted_recently("bot", post, persist_pruned=False)
    assert not (tmp_path / "recent_bot.json").exists()


def test_filter_recent_posts_no_persist_does_not_create_file(monkeypatch, tmp_path):
    _patch_files(monkeypatch, tmp_path)
    post = _make_post()
    recent_post_guard.filter_recent_posts("bot", [post], persist_pruned=False)
    assert not (tmp_path / "recent_bot.json").exists()


# ---------------------------------------------------------------------------
# record_published_post refreshes timestamp on re-record
# ---------------------------------------------------------------------------

def test_re_recording_refreshes_timestamp(monkeypatch, tmp_path):
    _patch_files(monkeypatch, tmp_path)
    post = _make_post()
    old_ts = time.time() - (27 * 24 * 60 * 60)  # just inside 28-day window
    posts_file = str(tmp_path / "recent_bot.json")
    sig = recent_post_guard._signature_key(post)
    write_json_atomic(posts_file, [{"signature": sig, "recorded_at": old_ts, "bot": "bot", "post_type": "photo"}])

    recent_post_guard.record_published_post("bot", post)

    from json_storage import read_json_file
    entries = read_json_file(posts_file, default=[], expected_type=list)
    matching = [e for e in entries if e["signature"] == sig]
    assert len(matching) == 1
    assert matching[0]["recorded_at"] > old_ts
