"""Tests for try_telegram retry behavior in the notifier bot.

Regression coverage for the retry path consuming the upload stream: a failed
send leaves the BytesIO at EOF, so retrying without rewinding uploads 0 bytes
and Telegram answers "Bad Request: file must be non-empty".
"""

import io

import pytest
from requests import exceptions as requests_exceptions

import imageboard_notifier_bot as notifierbot

NOTIFIERS = [pytest.param(notifierbot, id="notifier")]


@pytest.mark.parametrize("module", NOTIFIERS)
def test_retry_rewinds_consumed_media_stream(module, monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    media = io.BytesIO(b"image-bytes")
    seen = []

    def flaky_send(stream):
        # requests drains the stream before the transport error surfaces.
        seen.append(stream.read())
        if len(seen) == 1:
            raise requests_exceptions.ConnectionError("connection reset")
        return "sent"

    assert module.try_telegram("upload_photo", flaky_send, media) == "sent"
    assert seen == [b"image-bytes", b"image-bytes"]


@pytest.mark.parametrize("module", NOTIFIERS)
def test_retry_rewinds_media_passed_as_keyword(module, monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    media = io.BytesIO(b"gif-bytes")
    seen = []

    def flaky_send(chat_id, *, photo):
        seen.append(photo.read())
        if len(seen) == 1:
            raise requests_exceptions.ConnectionError("reset by peer")
        return "sent"

    assert module.try_telegram("upload_photo", flaky_send, 1, photo=media) == "sent"
    assert seen == [b"gif-bytes", b"gif-bytes"]


@pytest.mark.parametrize("module", NOTIFIERS)
def test_transient_failure_gives_up_after_configured_attempts(module, monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    calls = []

    def always_timeout():
        calls.append(1)
        raise requests_exceptions.ConnectTimeout("connect timed out")

    assert module.try_telegram("upload_photo", always_timeout) is None
    assert len(calls) == module.SEND_RETRY_ATTEMPTS + 1


@pytest.mark.parametrize("module", NOTIFIERS)
def test_read_timeout_is_not_retried_in_place(module, monkeypatch):
    # Tras un ReadTimeout Telegram pudo haber publicado la foto: reintentar
    # la subida en el acto la duplicaria.
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    calls = []

    def lost_response():
        calls.append(1)
        raise requests_exceptions.ReadTimeout("read timed out")

    result, error = module.try_telegram_detailed("upload_photo", lost_response)

    assert result is None
    assert isinstance(error, requests_exceptions.ReadTimeout)
    assert len(calls) == 1


@pytest.mark.parametrize("module", NOTIFIERS)
def test_non_transient_error_is_not_retried(module, monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    calls = []

    def rejected():
        calls.append(1)
        raise ValueError("bad payload")

    assert module.try_telegram("upload_photo", rejected) is None
    assert len(calls) == 1


@pytest.mark.parametrize("module", NOTIFIERS)
def test_rewind_ignores_non_seekable_arguments(module):
    class NoSeek:
        def read(self):
            return b""

        def seek(self, _position):
            raise OSError("not seekable")

    # Must not raise: unseekable payloads simply skip the rewind.
    module.rewind_media_arguments((NoSeek(), "caption", 7), {"reply_markup": None})
