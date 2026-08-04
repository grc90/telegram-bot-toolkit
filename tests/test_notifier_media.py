import imageboard_notifier_bot as dangbot


def test_photo_media_urls_prefers_quality_variants():
    post = {
        "file_url": "https://example.test/original.jpg",
        "large_file_url": "https://example.test/large.jpg",
        "preview_file_url": "https://example.test/preview.jpg",
    }

    # El thumbnail de 180x180 nunca es candidato de envio.
    assert dangbot.media_urls(post) == [
        "https://example.test/original.jpg",
        "https://example.test/large.jpg",
    ]
    assert dangbot.photo_media_urls(post) == [
        "https://example.test/original.jpg",
        "https://example.test/large.jpg",
    ]


def test_photo_media_urls_deduplicates_variants():
    post = {
        "file_url": "https://example.test/image.jpg",
        "large_file_url": "https://example.test/image.jpg",
        "preview_file_url": "https://example.test/preview.jpg",
    }

    assert dangbot.photo_media_urls(post) == ["https://example.test/image.jpg"]


def test_preview_only_post_has_no_sendable_variant():
    post = {"preview_file_url": "https://example.test/preview.jpg"}

    assert dangbot.photo_media_urls(post) == []
