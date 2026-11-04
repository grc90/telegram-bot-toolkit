import pytest

from queue_dedupe import dedupe_queue, normalize_source_url, post_signature, strip_twitter_tracking


def test_dedupe_queue_uses_normalized_source_url():
    queue = [
        {"type": "photo", "source_url": "https://twitter.com/example/status/1?x=1", "file_id": "a"},
        {"type": "photo", "source_url": "https://x.com/example/status/1", "file_id": "b"},
    ]

    assert len(dedupe_queue(queue)) == 1


def test_post_signature_handles_malformed_items():
    assert post_signature("bad")[0] == "invalid"
    assert len(dedupe_queue(["bad", "bad"])) == 1


def test_normalize_source_url_strips_query_and_fragment():
    assert normalize_source_url("HTTPS://Example.com/path/?a=1#frag") == "https://example.com/path"


def test_normalize_source_url_keeps_query_for_dcinside():
    url_a = "https://gall.dcinside.com/mgallery/board/view?id=projectmx&no=18340149"
    url_b = "https://gall.dcinside.com/mgallery/board/view?id=projectmx&no=99999999"

    assert normalize_source_url(url_a) == url_a
    assert normalize_source_url(url_a) != normalize_source_url(url_b)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://x.com/x3li9/status/2116335963693551890?a=PwqO8lGfYYWgrsZh5Eg&s=20",
            "https://x.com/x3li9/status/2116335963693551890",
        ),
        ("https://twitter.com/a/status/1?s=20&t=abc", "https://twitter.com/a/status/1"),
        ("https://mobile.twitter.com/a/status/1?s=20", "https://mobile.twitter.com/a/status/1"),
        ("https://WWW.X.com/a/status/1?s=20#m", "https://WWW.X.com/a/status/1"),
        ("https://x.com/a/status/1", "https://x.com/a/status/1"),
    ],
)
def test_strip_twitter_tracking_removes_query_from_twitter_links(url, expected):
    assert strip_twitter_tracking(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://gall.dcinside.com/mgallery/board/view?id=projectmx&no=18340149",
        "https://www.pixiv.net/artworks/1?a=1",
        "https://notx.com/a?s=20",
        "not a url?s=20",
    ],
)
def test_strip_twitter_tracking_leaves_other_urls_untouched(url):
    assert strip_twitter_tracking(url) == url
