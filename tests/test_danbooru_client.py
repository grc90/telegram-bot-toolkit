import io
import json
import urllib.error

import pytest

import danbooru_client
from danbooru_client import DanbooruResponseError, json_request, validate_post


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
