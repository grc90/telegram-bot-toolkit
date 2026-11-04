import io
import json
import urllib.error

import pytest

import danbooru_client
from danbooru_client import DanbooruResponseError, is_drive_source, is_telegram_source, json_request, validate_post


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def test_json_request_retries_transient_http_error(monkeypatch):
    calls = {"count": 0}

    def fake_urlopen(request, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, io.BytesIO())
        return FakeResponse(json.dumps([{"id": 1}]).encode("utf-8"))

    monkeypatch.setattr(danbooru_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(danbooru_client.time, "sleep", lambda seconds: None)

    result = json_request(
        "https://example.test",
        "/posts.json",
        {"tags": "rating:g"},
        user_agent="test",
        retry_attempts=1,
    )

    assert result == [{"id": 1}]
    assert calls["count"] == 2


def test_json_request_rejects_wrong_shape(monkeypatch):
    monkeypatch.setattr(
        danbooru_client.urllib.request,
        "urlopen",
        lambda request, timeout: FakeResponse(json.dumps({"id": 1}).encode("utf-8")),
    )

    with pytest.raises(DanbooruResponseError):
        json_request("https://example.test", "/posts.json", {}, user_agent="test")


def test_validate_post_rejects_missing_rating_and_media_url():
    assert validate_post({"id": 1, "file_url": "https://example.test/a.jpg"})[0] is False
    assert validate_post({"id": 1, "rating": "g"}, allowed_ratings={"g"})[0] is False
    assert validate_post({"id": 1, "rating": "q", "file_url": "https://example.test/a.jpg"}, allowed_ratings={"g"})[0] is False


def test_is_telegram_source_matches_telegram_links():
    assert is_telegram_source("https://t.me/SomeChannel/123") is True
    assert is_telegram_source("  http://T.ME/SomeChannel  ") is True
    assert is_telegram_source("t.me/SomeChannel") is True
    assert is_telegram_source("https://telegram.me/SomeChannel") is True
    assert is_telegram_source("https://cdn4.telesco.pe/file/abc.jpg") is True


def test_is_telegram_source_ignores_other_sources():
    assert is_telegram_source("") is False
    assert is_telegram_source(None) is False
    assert is_telegram_source("https://www.pixiv.net/artworks/123") is False
    assert is_telegram_source("https://twitter.com/user/status/1") is False
    # No basta con que "t.me" aparezca dentro del host o de la ruta.
    assert is_telegram_source("https://not-t.me.example.test/a") is False
    assert is_telegram_source("https://example.test/t.me/a") is False


def test_validate_post_rejects_telegram_source():
    post = {
        "id": 1,
        "rating": "g",
        "file_url": "https://example.test/a.jpg",
        "source": "https://t.me/SomeChannel/123",
    }
    is_valid, reason = validate_post(post, allowed_ratings={"g"})

    assert is_valid is False
    assert "Telegram" in reason
    assert validate_post({**post, "source": "https://www.pixiv.net/artworks/1"}, allowed_ratings={"g"})[0] is True


def test_is_drive_source_matches_drive_links():
    assert is_drive_source("https://drive.google.com/file/d/abc123/view") is True
    assert is_drive_source("  http://DRIVE.GOOGLE.COM/open?id=abc  ") is True
    assert is_drive_source("drive.google.com/open?id=abc") is True
    assert is_drive_source("https://docs.google.com/document/d/abc") is True
    assert is_drive_source("https://drive.usercontent.google.com/download?id=abc") is True


def test_is_drive_source_ignores_other_sources():
    assert is_drive_source("") is False
    assert is_drive_source(None) is False
    assert is_drive_source("https://www.pixiv.net/artworks/123") is False
    assert is_drive_source("https://twitter.com/user/status/1") is False
    assert is_drive_source("https://not-drive.google.com.example.test/a") is False
    assert is_drive_source("https://example.test/drive.google.com/a") is False


def test_validate_post_rejects_drive_source():
    post = {
        "id": 1,
        "rating": "g",
        "file_url": "https://example.test/a.jpg",
        "source": "https://drive.google.com/file/d/abc123/view",
    }
    is_valid, reason = validate_post(post, allowed_ratings={"g"})

    assert is_valid is False
    assert "Drive" in reason
    assert validate_post({**post, "source": "https://www.pixiv.net/artworks/1"}, allowed_ratings={"g"})[0] is True
