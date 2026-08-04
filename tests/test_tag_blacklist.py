from tag_blacklist import BLACKLISTED_TAGS, has_blacklisted_tag


def test_shared_blacklist_contains_literal_filters():
    assert "ai-generated" in BLACKLISTED_TAGS
    assert "animated" in BLACKLISTED_TAGS


def test_has_blacklisted_tag_matches_any_tag_string_field():
    # The API splits tags across tag_string, tag_string_character,
    # tag_string_artist, ... — every one of them has to be checked.
    post = {
        "tag_string": "safe_tag",
        "tag_string_general": "ai-generated",
    }

    assert has_blacklisted_tag(post)


def test_has_blacklisted_tag_honors_wildcard_patterns():
    post = {"tag_string": "some_reaction_(meme)"}

    assert has_blacklisted_tag(post)


def test_clean_post_is_not_blacklisted():
    post = {
        "tag_string": "1girl outdoors",
        "tag_string_artist": "someone",
    }

    assert not has_blacklisted_tag(post)
