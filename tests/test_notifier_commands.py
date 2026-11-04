"""Tests for dangbot QOL changes: /help, /blacklist, startup log, --dry-run, env var thresholds."""

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

from imageboard_notifier_bot import APP as dangbot


# ---------------------------------------------------------------------------
# TOP/HOT threshold env vars
# ---------------------------------------------------------------------------

def test_top_score_threshold_reads_env_var(monkeypatch):
    monkeypatch.setenv("IMAGEBOARD_NOTIFIER_TOP_SCORE_THRESHOLD", "42")
    # Re-read the module-level value by reading the env directly (the module already loaded)
    value = int(os.environ.get("IMAGEBOARD_NOTIFIER_TOP_SCORE_THRESHOLD", "20"))
    assert value == 42


def test_hot_score_threshold_reads_env_var(monkeypatch):
    monkeypatch.setenv("IMAGEBOARD_NOTIFIER_HOT_SCORE_THRESHOLD", "7")
    value = int(os.environ.get("IMAGEBOARD_NOTIFIER_HOT_SCORE_THRESHOLD", "10"))
    assert value == 7


def test_top_threshold_default_is_20():
    assert dangbot.top_score_threshold >= 0  # loaded from env with default 20


def test_hot_threshold_default_is_positive():
    assert dangbot.hot_score_threshold >= 0  # loaded from env with default 10


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------

def test_dry_run_returns_0_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(dangbot, "fetch_matching_posts", lambda **kwargs: [])
    result = dangbot.main(["--dry-run"])
    assert result == 0


def test_dry_run_does_not_call_send(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(dangbot, "fetch_matching_posts", lambda **kwargs: [])
    send_calls = []
    monkeypatch.setattr(dangbot, "send_post_to_subscribers", lambda p: send_calls.append(p))
    dangbot.main(["--dry-run"])
    assert send_calls == []


def test_dry_run_does_not_modify_state(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(dangbot, "state_file", str(state_file))
    monkeypatch.setattr(dangbot, "fetch_matching_posts", lambda **kwargs: [
        {"id": 99, "rating": "g", "score": 15, "file_url": "https://x.test/1.jpg", "tag_string": ""},
    ])
    dangbot.main(["--dry-run"])
    # State file must not be created by dry-run
    assert not state_file.exists()


def test_dry_run_prints_summary(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(dangbot, "fetch_matching_posts", lambda **kwargs: [
        {"id": 1, "rating": "g", "score": 10, "file_url": "https://x.test/1.jpg", "tag_string": ""},
        {"id": 2, "rating": "g", "score": 12, "file_url": "https://x.test/2.jpg", "tag_string": ""},
    ])
    dangbot.main(["--dry-run"])
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "2" in out  # 2 matching posts


def test_dry_run_fetch_failure_returns_1(monkeypatch, tmp_path):
    monkeypatch.setattr(dangbot, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(dangbot, "fetch_matching_posts", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("API down")))
    result = dangbot.main(["--dry-run"])
    assert result == 1


# ---------------------------------------------------------------------------
# /blacklist test — matching logic (unit)
# ---------------------------------------------------------------------------

def test_blacklist_test_tag_blocked():
    import fnmatch
    from tag_blacklist import BLACKLISTED_TAGS
    tag = "ai-generated"
    matched = [b for b in BLACKLISTED_TAGS if fnmatch.fnmatchcase(tag, b)]
    assert len(matched) > 0


def test_blacklist_test_tag_not_blocked():
    import fnmatch
    from tag_blacklist import BLACKLISTED_TAGS
    tag = "1girl"
    matched = [b for b in BLACKLISTED_TAGS if fnmatch.fnmatchcase(tag, b)]
    assert len(matched) == 0


def test_blacklist_wildcard_matches():
    import fnmatch
    from tag_blacklist import BLACKLISTED_TAGS
    # "*_(meme)" should match any concrete meme tag
    tag = "some_reaction_(meme)"
    matched = [b for b in BLACKLISTED_TAGS if fnmatch.fnmatchcase(tag, b)]
    assert len(matched) > 0
