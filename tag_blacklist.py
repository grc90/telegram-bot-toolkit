"""Wildcard tag blacklist shared by the imageboard bots.

A post is rejected when any of its tags matches any pattern below. Patterns are
matched with `fnmatch`, so `*_suffix` / `prefix_*` work as you would expect —
that is the whole reason this is a set of patterns instead of a set of literals.

The list here is a short illustrative sample. A real deployment keeps a much
longer list tuned to what its channel does and does not want; treat this as the
shape of the data, not as a recommended filter.
"""

import fnmatch

BLACKLISTED_TAGS = {
    # Machine-generated art
    "ai-generated",
    "ai-assisted",
    "nai_diffusion",
    # Formats the photo pipeline cannot deliver well
    "animated",
    "game_cg",
    # Low-quality or unwanted sources
    "sample_watermark",
    "spoilers",
    # Wildcard patterns: every tag ending in "_(meme)" is rejected
    "*_(meme)",
}


def post_tags(post):
    tags = set()

    for key, value in post.items():
        if key.startswith("tag_string") and isinstance(value, str):
            tags.update(tag for tag in value.split() if tag)

    return tags


def has_blacklisted_tag(post):
    for tag in post_tags(post):
        for blocked_tag in BLACKLISTED_TAGS:
            if fnmatch.fnmatchcase(tag, blocked_tag):
                return True

    return False
