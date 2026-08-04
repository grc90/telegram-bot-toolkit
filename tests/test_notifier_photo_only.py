"""Los notifiers solo entregan fotos de calidad.

Garantias cubiertas aca:
  * nunca se llama send_document (nada llega como archivo),
  * nunca se usa preview_file_url (el thumbnail de 180x180),
  * un post que Telegram no acepta como foto y no tiene fallback se reporta con
    el link a Danbooru y queda marcado como visto,
  * un fallo de red no avisa nada y se reintenta en el proximo ciclo.
"""

import io

import pytest
from requests import exceptions as requests_exceptions

import imageboard_notifier_bot as notifierbot

NOTIFIERS = [pytest.param(notifierbot, id="notifier")]

POST = {
    "id": 11866149,
    "rating": "g",
    "score": 30,
    "file_ext": "jpg",
    "source": "https://example.test/art",
    "file_url": "https://cdn.test/original.jpg",
    "large_file_url": "https://cdn.test/sample.jpg",
    "preview_file_url": "https://cdn.test/preview.jpg",
}


class SendRecorder:
    """Sustituye bot.send_* y registra que se intento enviar."""

    def __init__(self, *, photo_result="ok", photo_error=None):
        self.calls = []
        self.captions = []
        self.photo_result = photo_result
        self.photo_error = photo_error

    def send_photo(self, chat_id, media, caption=None):
        self.calls.append(("send_photo", media))
        self.captions.append(caption)
        if self.photo_error is not None:
            raise self.photo_error
        return self.photo_result

    def send_document(self, chat_id, media, caption=None):
        self.calls.append(("send_document", media))
        return "ok"

    def send_message(self, chat_id, text, *args, **kwargs):
        self.calls.append(("send_message", text))
        return "ok"

    def send_animation(self, chat_id, media, caption=None):
        self.calls.append(("send_animation", media))
        return "ok"

    def send_video(self, chat_id, media, caption=None):
        self.calls.append(("send_video", media))
        return "ok"

    @property
    def kinds(self):
        return [kind for kind, _payload in self.calls]


def install(module, monkeypatch, recorder, *, downloads=None):
    monkeypatch.setattr(module, "bot", recorder)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    # call_telegram no envia nada mientras el cooldown de API este activo.
    monkeypatch.setattr(module, "api_cooldown_until", 0)

    requested = []

    def fake_download(url):
        requested.append(url)
        if downloads is None:
            return io.BytesIO(b"x" * 1024)

        payload = downloads.get(url)
        if isinstance(payload, Exception):
            raise payload
        if payload is None:
            raise ValueError(f"unexpected download: {url}")
        return io.BytesIO(payload)

    monkeypatch.setattr(module, "download_media", fake_download)
    return requested


@pytest.mark.parametrize("module", NOTIFIERS)
def test_good_photo_is_sent_from_the_original_variant(module, monkeypatch):
    recorder = SendRecorder()
    requested = install(module, monkeypatch, recorder)

    assert module.send_post_to_chat(1, POST) == module.SEND_RESULT_SENT
    assert recorder.kinds == ["send_photo"]
    assert requested == ["https://cdn.test/original.jpg"]


@pytest.mark.parametrize("module", NOTIFIERS)
def test_oversized_original_falls_back_to_sample_never_to_document(module, monkeypatch):
    oversized = b"x" * (module.TELEGRAM_PHOTO_MAX_BYTES + 1)
    recorder = SendRecorder()
    requested = install(
        module,
        monkeypatch,
        recorder,
        downloads={
            "https://cdn.test/original.jpg": oversized,
            "https://cdn.test/sample.jpg": b"sample-bytes",
        },
    )

    assert module.send_post_to_chat(1, POST) == module.SEND_RESULT_SENT
    assert recorder.kinds == ["send_photo"]
    assert requested == ["https://cdn.test/original.jpg", "https://cdn.test/sample.jpg"]
    assert "send_document" not in recorder.kinds


@pytest.mark.parametrize("module", NOTIFIERS)
def test_unsendable_post_reports_the_danbooru_link_and_never_a_document(module, monkeypatch):
    oversized = b"x" * (module.TELEGRAM_PHOTO_MAX_BYTES + 1)
    recorder = SendRecorder()
    install(
        module,
        monkeypatch,
        recorder,
        downloads={
            "https://cdn.test/original.jpg": oversized,
            "https://cdn.test/sample.jpg": oversized,
        },
    )

    assert module.send_post_to_chat(1, POST) == module.SEND_RESULT_SKIPPED
    # Sin fallback posible: llega el link a Danbooru, nunca un archivo.
    assert recorder.kinds == ["send_message"]
    assert module.post_url(POST) in recorder.calls[0][1]
    assert "send_document" not in recorder.kinds


@pytest.mark.parametrize("module", NOTIFIERS)
def test_preview_variant_is_never_downloaded_or_sent(module, monkeypatch):
    recorder = SendRecorder()
    requested = install(
        module,
        monkeypatch,
        recorder,
        downloads={
            "https://cdn.test/original.jpg": b"",
            "https://cdn.test/sample.jpg": b"",
        },
    )

    module.send_post_to_chat(1, POST)
    assert "https://cdn.test/preview.jpg" not in requested


@pytest.mark.parametrize("module", NOTIFIERS)
def test_network_failure_reports_failed_so_the_post_is_retried(module, monkeypatch):
    recorder = SendRecorder(photo_error=requests_exceptions.ReadTimeout("read timed out"))
    install(module, monkeypatch, recorder)

    assert module.send_post_to_chat(1, POST) == module.SEND_RESULT_FAILED
    assert "send_document" not in recorder.kinds
    assert "send_message" not in recorder.kinds


@pytest.mark.parametrize("module", NOTIFIERS)
def test_non_photo_extension_is_skipped_instead_of_sent_as_document(module, monkeypatch):
    recorder = SendRecorder()
    install(module, monkeypatch, recorder)
    zip_post = dict(POST, file_ext="zip", file_url="https://cdn.test/archive.zip")

    assert module.send_post_to_chat(1, zip_post) == module.SEND_RESULT_SKIPPED
    assert recorder.calls == []


@pytest.mark.parametrize("module", NOTIFIERS)
def test_post_without_usable_media_url_is_skipped(module, monkeypatch):
    recorder = SendRecorder()
    install(module, monkeypatch, recorder)
    preview_only = {"id": 1, "file_ext": "jpg", "preview_file_url": "https://cdn.test/preview.jpg"}

    assert module.send_post_to_chat(1, preview_only) == module.SEND_RESULT_SKIPPED
    assert recorder.calls == []


@pytest.mark.parametrize("module", NOTIFIERS)
def test_no_notifier_source_still_references_the_removed_fallback(module):
    import inspect

    source = inspect.getsource(module)
    assert "No pude enviar este post como foto" not in source
    assert "send_document" not in source


# ---------------------------------------------------------------------------
# Pre-filtrado por metadata: evita descargar/subir variantes que Telegram
# rechazaria con PHOTO_INVALID_DIMENSIONS o por tamano.
# ---------------------------------------------------------------------------

# Datos reales del post 11876773, que disparo PHOTO_INVALID_DIMENSIONS.
HUGE_POST = {
    "id": 11876773,
    "rating": "g",
    "file_ext": "jpg",
    "image_width": 9346,
    "image_height": 11934,
    "file_size": 22227512,
    "file_url": "https://cdn.donmai.us/original/28/ba/28ba.jpg",
    "large_file_url": "https://cdn.donmai.us/sample/28/ba/sample-28ba.jpg",
    "media_asset": {
        "variants": [
            {"type": "sample", "url": "https://cdn.donmai.us/sample/28/ba/sample-28ba.jpg",
             "width": 850, "height": 1085},
            {"type": "original", "url": "https://cdn.donmai.us/original/28/ba/28ba.jpg",
             "width": 9346, "height": 11934},
        ]
    },
}


@pytest.mark.parametrize("module", NOTIFIERS)
def test_oversized_original_is_never_downloaded(module, monkeypatch):
    recorder = SendRecorder()
    requested = install(
        module,
        monkeypatch,
        recorder,
        downloads={"https://cdn.donmai.us/sample/28/ba/sample-28ba.jpg": b"sample-bytes"},
    )

    assert module.send_post_to_chat(1, HUGE_POST) == module.SEND_RESULT_SENT
    # El original (9346x11934, 22 MB) se descarta por metadata, sin bajarlo.
    assert requested == ["https://cdn.donmai.us/sample/28/ba/sample-28ba.jpg"]
    assert recorder.kinds == ["send_photo"]


@pytest.mark.parametrize("module", NOTIFIERS)
def test_rejection_reasons_match_telegram_photo_limits(module):
    limit = module.TELEGRAM_PHOTO_MAX_TOTAL_DIMENSION
    ratio = module.TELEGRAM_PHOTO_MAX_ASPECT_RATIO

    assert module.photo_variant_rejection(850, 1085, None) == ""
    assert "width+height" in module.photo_variant_rejection(limit, 1, None)
    assert "aspect ratio" in module.photo_variant_rejection(ratio * 100 + 1, 100, None)
    assert "photo limit" in module.photo_variant_rejection(
        800, 600, module.TELEGRAM_PHOTO_MAX_BYTES + 1
    )
    # Sin metadata se intenta igual en vez de descartar de mas.
    assert module.photo_variant_rejection(None, None, None) == ""


@pytest.mark.parametrize("module", NOTIFIERS)
def test_dimension_rejection_reports_the_link_without_downloading(module, monkeypatch):
    # Original 9346x11934 y sample igual de imposible: no queda fallback.
    impossible = dict(
        HUGE_POST,
        media_asset={
            "variants": [
                {"type": "sample", "url": HUGE_POST["large_file_url"], "width": 9000, "height": 9000},
                {"type": "original", "url": HUGE_POST["file_url"], "width": 9346, "height": 11934},
            ]
        },
    )
    recorder = SendRecorder()
    requested = install(module, monkeypatch, recorder, downloads={})

    assert module.send_post_to_chat(1, impossible) == module.SEND_RESULT_SKIPPED
    assert requested == []
    assert recorder.kinds == ["send_message"]
    notice = recorder.calls[0][1]
    assert module.post_url(impossible) in notice
    assert "width+height" in notice


@pytest.mark.parametrize("module", NOTIFIERS)
def test_transient_failure_stays_silent_because_it_will_be_retried(module, monkeypatch):
    recorder = SendRecorder(photo_error=requests_exceptions.ReadTimeout("read timed out"))
    install(module, monkeypatch, recorder)

    assert module.send_post_to_chat(1, POST) == module.SEND_RESULT_FAILED
    # Nada de avisos por un corte de red: el post se reintenta solo.
    assert "send_message" not in recorder.kinds


@pytest.mark.parametrize("module", NOTIFIERS)
def test_downgraded_photo_keeps_the_plain_caption(module, monkeypatch):
    # El original excede los limites y se entrega el sample: como la foto SI
    # llega, la caption no se ensucia con el link a Danbooru.
    # Con source propio la caption es solo el source: si el link de Danbooru
    # apareciera seria porque se agrego de mas.
    huge_with_source = dict(HUGE_POST, source="https://www.pixiv.net/artworks/147687176")
    recorder = SendRecorder()
    install(
        module,
        monkeypatch,
        recorder,
        downloads={HUGE_POST["large_file_url"]: b"sample-bytes"},
    )

    assert module.send_post_to_chat(1, huge_with_source) == module.SEND_RESULT_SENT
    assert recorder.kinds == ["send_photo"]
    assert recorder.captions[0] == "https://www.pixiv.net/artworks/147687176"
    assert module.post_url(huge_with_source) not in recorder.captions[0]


@pytest.mark.parametrize("module", NOTIFIERS)
def test_full_quality_photo_caption_is_left_alone(module, monkeypatch):
    recorder = SendRecorder()
    install(module, monkeypatch, recorder)

    assert module.send_post_to_chat(1, POST) == module.SEND_RESULT_SENT
    assert recorder.captions[0] == module.build_caption(POST)
