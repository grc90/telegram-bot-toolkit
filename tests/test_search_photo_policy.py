"""The search bot sends photos with the notifiers' quality policy (original first, no preview, no document)."""

import io
from unittest.mock import Mock

import telebot
from requests import exceptions as requests_exceptions

import imageboard_search_bot as search_bot


POST = {
    "id": 1,
    "rating": "g",
    "file_ext": "jpg",
    "source": "https://example.test/art",
    "file_url": "https://cdn.test/original.jpg",
    "large_file_url": "https://cdn.test/sample.jpg",
    "preview_file_url": "https://cdn.test/preview.jpg",
}


def _rejection():
    return telebot.apihelper.ApiTelegramException(
        "sendPhoto", Mock(), {"error_code": 400, "description": "Bad Request: wrong file identifier/HTTP URL specified"}
    )


class Recorder:
    def __init__(self, outcomes):
        # outcomes: lista de resultados por llamada a send_photo (excepcion o valor).
        self.outcomes = list(outcomes)
        self.photo_calls = []
        self.document_calls = []

    def send_photo(self, chat_id, media, caption=None):
        self.photo_calls.append(media if isinstance(media, str) else "upload")
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def send_document(self, *args, **kwargs):
        self.document_calls.append(args)
        return "ok"


def _install(monkeypatch, recorder, downloads=None):
    monkeypatch.setattr(search_bot, "bot", recorder)
    requested = []

    def fake_download(url):
        requested.append(url)
        payload = (downloads or {}).get(url, b"x" * 1024)
        if isinstance(payload, Exception):
            raise payload
        return io.BytesIO(payload)

    monkeypatch.setattr(search_bot, "download_media", fake_download)
    return requested


def test_original_goes_by_url_first(monkeypatch):
    recorder = Recorder(["ok"])
    requested = _install(monkeypatch, recorder)

    assert search_bot.send_post_to_chat(1, POST) == (True, None)
    assert recorder.photo_calls == ["https://cdn.test/original.jpg"]
    assert requested == []


def test_rejected_url_is_downloaded_and_uploaded(monkeypatch):
    recorder = Recorder([_rejection(), "ok"])
    requested = _install(monkeypatch, recorder)

    assert search_bot.send_post_to_chat(1, POST) == (True, None)
    assert recorder.photo_calls == ["https://cdn.test/original.jpg", "upload"]
    assert requested == ["https://cdn.test/original.jpg"]


def test_original_over_url_limit_skips_straight_to_upload(monkeypatch):
    big = dict(POST, file_size=search_bot.TELEGRAM_PHOTO_URL_MAX_BYTES + 1)
    recorder = Recorder(["ok"])
    requested = _install(monkeypatch, recorder)

    assert search_bot.send_post_to_chat(1, big) == (True, None)
    assert recorder.photo_calls == ["upload"]
    assert requested == ["https://cdn.test/original.jpg"]


def test_original_over_photo_limit_falls_back_to_sample(monkeypatch):
    huge = dict(POST, file_size=search_bot.TELEGRAM_PHOTO_MAX_BYTES + 1)
    recorder = Recorder(["ok"])
    _install(monkeypatch, recorder)

    assert search_bot.send_post_to_chat(1, huge) == (True, None)
    assert recorder.photo_calls == ["https://cdn.test/sample.jpg"]


def test_timeout_is_not_retried_with_another_variant(monkeypatch):
    recorder = Recorder([requests_exceptions.ReadTimeout("lost")])
    requested = _install(monkeypatch, recorder)

    assert search_bot.send_post_to_chat(1, POST) == (False, "send_failed")
    assert recorder.photo_calls == ["https://cdn.test/original.jpg"]
    assert requested == []


def test_never_sends_preview_or_document(monkeypatch):
    recorder = Recorder([_rejection(), _rejection(), _rejection(), _rejection()])
    _install(monkeypatch, recorder)

    sent, reason = search_bot.send_post_to_chat(1, POST)

    assert sent is False
    assert reason == "send_failed"
    assert "https://cdn.test/preview.jpg" not in recorder.photo_calls
    assert recorder.document_calls == []


def test_preview_only_post_is_not_sent(monkeypatch):
    recorder = Recorder([])
    _install(monkeypatch, recorder)

    assert search_bot.send_post_to_chat(1, {"id": 2, "preview_file_url": "https://cdn.test/p.jpg"}) == (
        False,
        "no_media_url",
    )
    assert recorder.photo_calls == []
