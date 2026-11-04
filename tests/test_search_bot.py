import io
import urllib.error

import pytest

import imageboard_search_bot as danfetcherbot


@pytest.fixture(autouse=True)
def isolate_session_state_file(monkeypatch, tmp_path):
    """Never let session persistence touch the real imageboard_search_state.json."""

    monkeypatch.setattr(danfetcherbot, "SESSION_STATE_FILE", str(tmp_path / "imageboard_search_state.json"))


def make_http_error(code=500):
    return urllib.error.HTTPError("https://example.test/posts.json", code, "server error", {}, io.BytesIO())


def test_fetch_posts_retries_wildcard_with_exact_tag_suggestions(monkeypatch):
    requests = []

    def fake_danbooru_request(path, params):
        requests.append(params["tags"])

        if params["tags"].startswith("*e_breasts "):
            raise make_http_error()

        return [
            {
                "id": 123,
                "rating": "q",
                "file_url": "https://example.test/123.jpg",
                "tag_string": "huge_breasts",
            }
        ]

    monkeypatch.setattr(danfetcherbot, "danbooru_request", fake_danbooru_request)
    monkeypatch.setattr(danfetcherbot, "search_tags", lambda tag: ["huge_breasts", "*e_breasts"])
    monkeypatch.setattr(danfetcherbot, "log", lambda message: None)

    posts, stats = danfetcherbot.fetch_posts("*e_breasts", "<=2y", "s,q", "score")

    assert [post["id"] for post in posts] == [123]
    assert stats["raw"] == 1
    assert requests == [
        "*e_breasts age:<=2y rating:s,q order:score",
        "huge_breasts age:<=2y rating:s,q order:score",
    ]


def test_fetch_error_message_names_expensive_wildcard_http_failure():
    message = danfetcherbot.fetch_error_message(make_http_error(), "*e_breasts")

    assert "HTTP 500" in message
    assert "comodin" in message


def test_fetch_posts_for_query_counts_blacklisted_and_invalid_posts(monkeypatch):
    def fake_danbooru_request(path, params):
        return [
            {"id": 1, "rating": "g", "file_url": "https://example.test/1.jpg", "tag_string": "ai-generated"},
            {"id": 2, "rating": "g", "file_url": "https://example.test/2.jpg", "tag_string": "safe_tag"},
            {"id": 3, "rating": "g"},  # missing media url -> invalid
        ]

    monkeypatch.setattr(danfetcherbot, "danbooru_request", fake_danbooru_request)
    monkeypatch.setattr(danfetcherbot, "log", lambda message: None)

    posts, stats = danfetcherbot.fetch_posts_for_query("anything", "g")

    assert [post["id"] for post in posts] == [2]
    assert stats == {"raw": 3, "blacklisted": 1, "invalid": 1}


def test_describe_empty_result_calls_out_fully_blacklisted_results():
    message = danfetcherbot.describe_empty_result({"raw": 5, "blacklisted": 5, "invalid": 0})

    assert "5" in message
    assert "bloqueados" in message


def test_describe_empty_result_handles_truly_empty_search():
    message = danfetcherbot.describe_empty_result({"raw": 0, "blacklisted": 0, "invalid": 0})

    assert "mal escrita" in message


def test_describe_failures_groups_by_reason():
    message = danfetcherbot.describe_failures(["too_large", "too_large", "send_failed"])

    assert "2 por el archivo pesaba mas de lo permitido" in message
    assert "1 por Telegram rechazo el envio" in message


def test_describe_failures_empty_list_returns_empty_string():
    assert danfetcherbot.describe_failures([]) == ""


def test_help_command_mid_flow_rearms_current_step(monkeypatch):
    sent_texts = []
    monkeypatch.setattr(danfetcherbot, "send_text", lambda chat_id, text: sent_texts.append(text))

    registered = []
    monkeypatch.setattr(
        danfetcherbot.bot,
        "register_next_step_handler_by_chat_id",
        lambda chat_id, handler: registered.append(handler),
    )

    danfetcherbot.update_session(555, step="age", tag="foo")

    class FakeMessage:
        text = "/help"

        class chat:
            id = 555

        class from_user:
            id = danfetcherbot.ALLOWED_USER_ID

    handled = danfetcherbot.handle_in_flow_command(FakeMessage())

    assert handled is True
    assert registered == [danfetcherbot.handle_age_step]
    assert any("Uso:" in text for text in sent_texts)

    danfetcherbot.clear_session(555)


def make_fake_callback(chat_id, data, message_id=1):
    class FakeCall:
        pass

    FakeCall.data = data
    FakeCall.id = "cbid"

    class chat:
        id = chat_id

    class message:
        pass

    message.chat = chat
    message.message_id = message_id
    FakeCall.message = message

    class from_user:
        id = danfetcherbot.ALLOWED_USER_ID

    FakeCall.from_user = from_user
    return FakeCall()


def mock_callback_plumbing(monkeypatch):
    monkeypatch.setattr(danfetcherbot, "send_text", lambda chat_id, text: None)
    monkeypatch.setattr(danfetcherbot.bot, "send_message", lambda *a, **k: None)
    monkeypatch.setattr(danfetcherbot.bot, "register_next_step_handler_by_chat_id", lambda chat_id, handler: None)
    monkeypatch.setattr(danfetcherbot.bot, "answer_callback_query", lambda call_id, text=None: None)
    monkeypatch.setattr(danfetcherbot.bot, "edit_message_reply_markup", lambda *a, **k: None)


def test_handle_tag_step_detects_multi_word_input_and_asks_to_disambiguate(monkeypatch):
    sent_texts = []
    monkeypatch.setattr(danfetcherbot, "send_text", lambda chat_id, text: sent_texts.append(text))
    monkeypatch.setattr(danfetcherbot.bot, "send_message", lambda *a, **k: None)
    monkeypatch.setattr(danfetcherbot.bot, "register_next_step_handler_by_chat_id", lambda chat_id, handler: None)

    def fail_search_tags(tag):
        raise AssertionError("search_tags should not run before the user disambiguates")

    monkeypatch.setattr(danfetcherbot, "search_tags", fail_search_tags)

    danfetcherbot.reset_session(777)

    class FakeMessage:
        text = "remilia scarlet"

        class chat:
            id = 777

        class from_user:
            id = danfetcherbot.ALLOWED_USER_ID

    danfetcherbot.handle_tag_step(FakeMessage())

    session = danfetcherbot.get_session(777)
    assert session["step"] == "tag_form"
    assert session["tag_form_compound"] == "remilia_scarlet"
    assert session["tag_form_combined"] == "remilia scarlet"
    assert any("varias palabras" in text for text in sent_texts)

    danfetcherbot.clear_session(777)


def test_tag_form_callback_combined_skips_suggestions_and_goes_to_age(monkeypatch):
    mock_callback_plumbing(monkeypatch)

    danfetcherbot.update_session(
        888, step="tag_form", tag_form_compound="remilia_scarlet", tag_form_combined="remilia scarlet"
    )

    danfetcherbot.handle_tag_form_callback(make_fake_callback(888, "tagform:combined"))

    session = danfetcherbot.get_session(888)
    assert session["tag"] == "remilia scarlet"
    assert session["step"] == "age"

    danfetcherbot.clear_session(888)


def test_tag_form_callback_compound_uses_compound_tag_for_suggestions(monkeypatch):
    mock_callback_plumbing(monkeypatch)

    search_calls = []
    monkeypatch.setattr(
        danfetcherbot, "search_tags", lambda tag: search_calls.append(tag) or ["remilia_scarlet"]
    )

    danfetcherbot.update_session(
        999, step="tag_form", tag_form_compound="remilia_scarlet", tag_form_combined="remilia scarlet"
    )

    danfetcherbot.handle_tag_form_callback(make_fake_callback(999, "tagform:compound"))

    assert search_calls == ["remilia_scarlet"]
    session = danfetcherbot.get_session(999)
    assert session["tag_query"] == "remilia_scarlet"
    assert session["tag_options"] == ["remilia_scarlet"]
    assert session["step"] == "tag"

    danfetcherbot.clear_session(999)


def test_tag_callback_rejects_stale_round_after_refinement(monkeypatch):
    mock_callback_plumbing(monkeypatch)
    monkeypatch.setattr(danfetcherbot, "search_tags", lambda tag: ["cat_a", "cat_b"])

    danfetcherbot.reset_session(1010)
    danfetcherbot.proceed_with_tag_query(1010, "cat")
    stale_callback_data = "tagsel:1:0"

    danfetcherbot.proceed_with_tag_query(1010, "dog")

    danfetcherbot.handle_tag_callback(make_fake_callback(1010, stale_callback_data))

    session = danfetcherbot.get_session(1010)
    assert session["tag"] == ""
    assert session["tag_round"] == 2

    danfetcherbot.clear_session(1010)


def test_tag_callback_accepts_current_round_selection(monkeypatch):
    mock_callback_plumbing(monkeypatch)
    monkeypatch.setattr(danfetcherbot, "search_tags", lambda tag: ["cat_a", "cat_b"])

    danfetcherbot.reset_session(1111)
    danfetcherbot.proceed_with_tag_query(1111, "cat")

    danfetcherbot.handle_tag_callback(make_fake_callback(1111, "tagsel:1:1"))

    session = danfetcherbot.get_session(1111)
    assert session["tag"] == "cat_b"
    assert session["step"] == "age"

    danfetcherbot.clear_session(1111)


def test_age_callback_selects_age_and_advances_to_rating(monkeypatch):
    mock_callback_plumbing(monkeypatch)
    danfetcherbot.update_session(2021, step="age", tag="cat")

    danfetcherbot.handle_age_callback(make_fake_callback(2021, "agesel:7d"))

    session = danfetcherbot.get_session(2021)
    assert session["age"] == "<=7d"
    assert session["step"] == "rating"

    danfetcherbot.clear_session(2021)


def test_age_callback_rejects_when_step_already_moved_on(monkeypatch):
    mock_callback_plumbing(monkeypatch)
    danfetcherbot.update_session(2023, step="rating", age="7d")

    danfetcherbot.handle_age_callback(make_fake_callback(2023, "agesel:24h"))

    session = danfetcherbot.get_session(2023)
    assert session["age"] == "7d"

    danfetcherbot.clear_session(2023)


def test_rating_callback_selects_rating_and_advances_to_order(monkeypatch):
    mock_callback_plumbing(monkeypatch)
    danfetcherbot.update_session(2022, step="rating", tag="cat", age="7d")

    danfetcherbot.handle_rating_callback(make_fake_callback(2022, "ratingsel:g,s"))

    session = danfetcherbot.get_session(2022)
    assert session["rating"] == "g,s"
    assert session["step"] == "order"

    danfetcherbot.clear_session(2022)


def test_order_callback_selects_order_and_starts_fetch(monkeypatch):
    mock_callback_plumbing(monkeypatch)
    fetch_calls = []
    monkeypatch.setattr(danfetcherbot, "start_fetch", lambda *args: fetch_calls.append(args))

    danfetcherbot.update_session(2020, step="order", tag="cat", age="7d", rating="g")

    danfetcherbot.handle_order_callback(make_fake_callback(2020, "ordersel:score"))

    assert fetch_calls == [(2020, "cat", "7d", "g", "score")]
    assert danfetcherbot.get_session(2020) == {}


def test_stepback_from_rating_returns_to_age(monkeypatch):
    mock_callback_plumbing(monkeypatch)
    danfetcherbot.update_session(2024, step="rating", tag="cat", age="7d")

    danfetcherbot.handle_step_back_callback(make_fake_callback(2024, "stepback:rating"))

    session = danfetcherbot.get_session(2024)
    assert session["step"] == "age"

    danfetcherbot.clear_session(2024)


def test_stepback_rejected_when_step_already_advanced(monkeypatch):
    mock_callback_plumbing(monkeypatch)
    danfetcherbot.update_session(2025, step="order", tag="cat")

    danfetcherbot.handle_step_back_callback(make_fake_callback(2025, "stepback:rating"))

    session = danfetcherbot.get_session(2025)
    assert session["step"] == "order"

    danfetcherbot.clear_session(2025)


def test_update_session_persists_state_to_disk():
    import json

    danfetcherbot.reset_session(3030)
    danfetcherbot.update_session(3030, step="age", tag="cat")

    with open(danfetcherbot.SESSION_STATE_FILE, encoding="utf-8") as file:
        record = json.load(file)

    assert record["chat_id"] == 3030
    assert record["session"]["step"] == "age"
    assert record["session"]["tag"] == "cat"

    danfetcherbot.clear_session(3030)


def test_clear_session_wipes_persisted_state():
    import json

    danfetcherbot.reset_session(3031)
    danfetcherbot.clear_session(3031)

    with open(danfetcherbot.SESSION_STATE_FILE, encoding="utf-8") as file:
        record = json.load(file)

    assert record == {}


def test_restore_persisted_session_rearms_pending_flow(monkeypatch):
    sent_texts = []
    monkeypatch.setattr(danfetcherbot, "send_text", lambda chat_id, text: sent_texts.append(text))

    registered = []
    monkeypatch.setattr(
        danfetcherbot.bot,
        "register_next_step_handler_by_chat_id",
        lambda chat_id, handler: registered.append((chat_id, handler)),
    )

    danfetcherbot.reset_session(4040)
    danfetcherbot.update_session(4040, step="rating", tag="cat", age="7d")
    danfetcherbot.chat_sessions.clear()  # simulate the process restarting and losing in-memory state

    danfetcherbot.restore_persisted_session()

    assert registered == [(4040, danfetcherbot.handle_rating_step)]
    session = danfetcherbot.get_session(4040)
    assert session["step"] == "rating"
    assert session["tag"] == "cat"
    assert any("busqueda sin terminar" in text for text in sent_texts)

    danfetcherbot.clear_session(4040)


def test_restore_persisted_session_noop_when_nothing_persisted(monkeypatch):
    registered = []
    monkeypatch.setattr(
        danfetcherbot.bot,
        "register_next_step_handler_by_chat_id",
        lambda chat_id, handler: registered.append((chat_id, handler)),
    )

    danfetcherbot.restore_persisted_session()

    assert registered == []


# ---------------------------------------------------------------------------
# Danbooru tag limit (2 tags without a Gold account; order: counts)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tags, expected",
    [
        ("remilia_scarlet", 1),
        ("remilia_scarlet flandre_scarlet", 2),
        ("-solo *_breasts ~cat", 3),
        ("cat rating:g age:<=1y score:>10 order:score", 2),
        ("re:zero_kara_hajimeru_isekai_seikatsu", 1),
        ("", 0),
    ],
)
def test_count_limited_tags(tags, expected):
    assert danfetcherbot.count_limited_tags(tags) == expected


def test_build_query_keeps_order_with_one_tag():
    assert danfetcherbot.build_query("cat", "<=1y", "g", "score") == "cat age:<=1y rating:g order:score"


def test_build_query_drops_order_when_two_tags_fill_the_limit():
    query = danfetcherbot.build_query("cat dog", "<=1y", "g", "score")

    assert query == "cat dog age:<=1y rating:g"
    assert danfetcherbot.count_limited_tags(query) == danfetcherbot.DANBOORU_TAG_LIMIT


def test_tag_form_callback_combined_rejects_more_tags_than_the_limit(monkeypatch):
    mock_callback_plumbing(monkeypatch)
    sent_texts = []
    monkeypatch.setattr(danfetcherbot, "send_text", lambda chat_id, text: sent_texts.append(text))

    danfetcherbot.update_session(
        1212, step="tag_form", tag_form_compound="a_b_c", tag_form_combined="a b c"
    )

    danfetcherbot.handle_tag_form_callback(make_fake_callback(1212, "tagform:combined"))

    session = danfetcherbot.get_session(1212)
    assert session["step"] == "tag"
    assert session["tag"] == ""
    assert any("hasta 2 tags" in text and "mandaste 3" in text for text in sent_texts)

    danfetcherbot.clear_session(1212)
