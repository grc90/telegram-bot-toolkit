"""Tests for the notifiers' max post age filter (no re-sends of old re-tagged posts)."""

from datetime import datetime, timedelta, timezone

import pytest

from imageboard_notifier_bot import APP as notifier_bot


NOTIFIERS = [
    pytest.param(notifier_bot, "g", id="notifier"),
]


def _post(post_id, rating, created_at):
    return {
        "id": post_id,
        "rating": rating,
        "score": 1000,
        "file_url": f"https://x.test/{post_id}.jpg",
        "tag_string": "",
        "created_at": created_at,
    }


def _iso_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_query_limits_post_age(module, rating):
    assert f"age:<={module.max_post_age_days}d" in module.tag_query


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_query_change_is_part_of_filter_signature(module, rating):
    # Cambiar la antiguedad maxima debe re-bootstrapear en vez de inundar el chat.
    assert f"age:<={module.max_post_age_days}d" in module.filter_signature()


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_fetch_matching_posts_drops_posts_older_than_max_age(module, rating, monkeypatch):
    max_age = module.max_post_age_days
    posts = [
        _post(1, rating, _iso_days_ago(max_age + 1)),
        _post(2, rating, _iso_days_ago(max_age - 1)),
        _post(3, rating, None),
    ]
    monkeypatch.setattr(module, "fetch_posts_by_query", lambda tag_query, **kwargs: posts)

    matched_ids = [post["id"] for post in module.fetch_matching_posts()]

    assert matched_ids == [2, 3]


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_is_within_max_post_age_boundary(module, rating):
    now = datetime(2026, 1, 31, tzinfo=timezone.utc)
    limit = now - timedelta(days=module.max_post_age_days)

    assert module.is_within_max_post_age({"created_at": limit.isoformat()}, now=now)
    assert not module.is_within_max_post_age(
        {"created_at": (limit - timedelta(seconds=1)).isoformat()}, now=now
    )
