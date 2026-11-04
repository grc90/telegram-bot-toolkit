import danbooru_client
from imageboard_notifier_bot import APP as dangbot
import danbooru_notifier_shared as shared


def test_photo_media_urls_prefers_quality_variants():
    post = {
        "file_url": "https://example.test/original.jpg",
        "large_file_url": "https://example.test/large.jpg",
        "preview_file_url": "https://example.test/preview.jpg",
    }

    assert shared.photo_media_urls(post) == [
        "https://example.test/original.jpg",
        "https://example.test/large.jpg",
    ]


def test_photo_media_urls_deduplicates_variants():
    post = {
        "file_url": "https://example.test/image.jpg",
        "large_file_url": "https://example.test/image.jpg",
        "preview_file_url": "https://example.test/preview.jpg",
    }

    assert shared.photo_media_urls(post) == ["https://example.test/image.jpg"]


def test_preview_only_post_has_no_sendable_variant():
    post = {"preview_file_url": "https://example.test/preview.jpg"}

    assert shared.photo_media_urls(post) == []


def test_client_media_url_uses_the_original_and_never_the_preview():
    # Politica unica para el notifier y el search: el thumbnail de 180x180 nunca
    # es candidato de envio.
    post = {
        "file_url": "https://example.test/original.jpg",
        "large_file_url": "https://example.test/large.jpg",
        "preview_file_url": "https://example.test/preview.jpg",
    }

    assert danbooru_client.media_url(post) == "https://example.test/original.jpg"
    assert danbooru_client.media_url({"preview_file_url": "https://example.test/preview.jpg"}) is None

