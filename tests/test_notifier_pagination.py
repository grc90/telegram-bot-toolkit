"""Tests for the notifiers' early-stop pagination and updated_at cursor."""

from datetime import datetime, timedelta, timezone

import pytest

from imageboard_notifier_bot import APP as notifier_bot
import danbooru_notifier_shared as shared


NOTIFIERS = [
    pytest.param(notifier_bot, "g", id="notifier"),
]

NOW = datetime.now(timezone.utc)


def _post(post_id, rating, updated_hours_ago):
    timestamp = (NOW - timedelta(hours=updated_hours_ago)).isoformat()
    return {
        "id": post_id,
        "rating": rating,
        "score": 1000,
        "file_url": f"https://x.test/{post_id}.jpg",
        "file_ext": "jpg",
        "tag_string": "",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _fake_danbooru(monkeypatch, module, pages):
    requested_pages = []

    def fake_request(path, params):
        requested_pages.append(params["page"])
        index = params["page"] - 1
        return pages[index] if index < len(pages) else []

    monkeypatch.setattr(module, "danbooru_request", fake_request)
    return requested_pages


# ---------------------------------------------------------------------------
# fetch_posts_by_query
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_requests_the_max_page_size(module, rating, monkeypatch):
    seen_limits = []
    monkeypatch.setattr(module, "danbooru_request", lambda path, params: seen_limits.append(params["limit"]) or [])

    list(module.fetch_posts_by_query("q"))

    assert seen_limits == [200]


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_without_updated_since_fetches_until_empty_page(module, rating, monkeypatch):
    pages = [[_post(1, rating, 1)], [_post(2, rating, 100)]]
    requested_pages = _fake_danbooru(monkeypatch, module, pages)

    posts = list(module.fetch_posts_by_query("q", pages=5))

    assert [post["id"] for post in posts] == [1, 2]
    assert requested_pages == [1, 2, 3]


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_updated_since_stops_after_page_reaching_older_posts(module, rating, monkeypatch):
    pages = [
        [_post(1, rating, 1), _post(2, rating, 2)],
        [_post(3, rating, 3), _post(4, rating, 10)],
        [_post(5, rating, 20)],
    ]
    requested_pages = _fake_danbooru(monkeypatch, module, pages)

    posts = list(module.fetch_posts_by_query("q", pages=5, updated_since=NOW - timedelta(hours=5)))

    # La pagina que cruza el corte se devuelve entera; la siguiente ya no se pide.
    assert [post["id"] for post in posts] == [1, 2, 3, 4]
    assert requested_pages == [1, 2]


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_consumer_break_skips_remaining_pages(module, rating, monkeypatch):
    pages = [[_post(1, rating, 1)], [_post(2, rating, 2)], [_post(3, rating, 3)]]
    requested_pages = _fake_danbooru(monkeypatch, module, pages)

    posts = module.fetch_top_score_posts(min_score=0, limit=1)

    assert [post["id"] for post in posts] == [1]
    assert requested_pages == [1]


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_score_band_stops_paging_past_cutoff(module, rating, monkeypatch):
    pages = [[_post(1, rating, 1)], [_post(2, rating, 7)], [_post(3, rating, 8)]]
    for page in pages:
        page[0]["score"] = 10
    requested_pages = _fake_danbooru(monkeypatch, module, pages)

    posts = module.fetch_recent_score_band_posts(min_score=10, max_score=20, hours=6)

    assert [post["id"] for post in posts] == [1]
    assert requested_pages == [1, 2]


# ---------------------------------------------------------------------------
# check_danbooru_once cursor
# ---------------------------------------------------------------------------

def _run_cycle(monkeypatch, module, posts, send_result):
    calls = []

    def fake_fetch(updated_since=None):
        calls.append(updated_since)
        return posts

    monkeypatch.setattr(module, "fetch_matching_posts", fake_fetch)
    monkeypatch.setattr(module, "send_post_to_subscribers", lambda post: send_result)
    module.check_danbooru_once("test")
    return calls[0]


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_bootstrap_fetches_everything_and_sets_cursor(module, rating, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "state_file", str(tmp_path / "state.json"))

    updated_since = _run_cycle(monkeypatch, module, [_post(1, rating, 1)], shared.SEND_RESULT_SENT)

    assert updated_since is None
    assert module.get_state()["updated_cursor"] is not None


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_later_cycles_page_back_to_cursor_minus_margin(module, rating, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "state_file", str(tmp_path / "state.json"))
    _run_cycle(monkeypatch, module, [], shared.SEND_RESULT_SENT)
    cursor = shared.parse_post_datetime(module.get_state()["updated_cursor"])

    updated_since = _run_cycle(monkeypatch, module, [], shared.SEND_RESULT_SENT)

    assert updated_since == cursor - timedelta(seconds=shared.UPDATED_CURSOR_MARGIN_SECONDS)


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_transient_failure_keeps_cursor_so_post_stays_reachable(module, rating, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "state_file", str(tmp_path / "state.json"))
    _run_cycle(monkeypatch, module, [], shared.SEND_RESULT_SENT)
    cursor_before = module.get_state()["updated_cursor"]

    _run_cycle(monkeypatch, module, [_post(7, rating, 1)], shared.SEND_RESULT_FAILED)

    state = module.get_state()
    assert state["updated_cursor"] == cursor_before
    assert "7" not in state["handled_post_ids"]


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_successful_cycle_advances_cursor(module, rating, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "state_file", str(tmp_path / "state.json"))
    _run_cycle(monkeypatch, module, [], shared.SEND_RESULT_SENT)
    cursor_before = module.get_state()["updated_cursor"]

    _run_cycle(monkeypatch, module, [_post(8, rating, 1)], shared.SEND_RESULT_SKIPPED)

    state = module.get_state()
    assert state["updated_cursor"] > cursor_before
    assert "8" in state["handled_post_ids"]


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_failed_fetch_keeps_cursor(module, rating, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "state_file", str(tmp_path / "state.json"))
    _run_cycle(monkeypatch, module, [], shared.SEND_RESULT_SENT)
    cursor_before = module.get_state()["updated_cursor"]

    monkeypatch.setattr(module, "consecutive_danbooru_failures", 0)
    monkeypatch.setattr(module, "danbooru_failure_alerted", False)
    monkeypatch.setattr(module, "send_operator_alert", lambda message: None)

    def failing_fetch(updated_since=None):
        raise RuntimeError("Danbooru down")

    monkeypatch.setattr(module, "fetch_matching_posts", failing_fetch)
    module.check_danbooru_once("test")

    assert module.get_state()["updated_cursor"] == cursor_before


@pytest.mark.parametrize("module, rating", NOTIFIERS)
def test_legacy_state_without_cursor_rebootstraps_instead_of_flooding(module, rating, monkeypatch, tmp_path):
    from json_storage import write_json_atomic

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(module, "state_file", str(state_file))
    legacy_state = shared.default_state()
    legacy_state.pop("updated_cursor")
    legacy_state.update(bootstrapped=True, filter_signature=module.filter_signature())
    write_json_atomic(state_file, legacy_state)
    sent = []
    monkeypatch.setattr(module, "fetch_matching_posts", lambda updated_since=None: [_post(9, rating, 1)])
    monkeypatch.setattr(module, "send_post_to_subscribers", lambda post: sent.append(post["id"]) or shared.SEND_RESULT_SENT)

    module.check_danbooru_once("test")

    state = module.get_state()
    assert sent == []
    assert "9" in state["handled_post_ids"]
    assert state["updated_cursor"] is not None
