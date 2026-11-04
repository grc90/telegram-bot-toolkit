"""Tests for the shared Telegram error helpers and their callers."""

from unittest.mock import Mock

import pytest
import telebot
from requests import exceptions as requests_exceptions

import imageboard_search_bot
from imageboard_notifier_bot import APP as notifier_bot
from telegram_common import describe_telegram_error, should_pause_for_telegram_error, telegram_retry_after_seconds
import danbooru_notifier_shared as shared


def _api_error(code, description, retry_after=None):
    result_json = {"error_code": code, "description": description}
    if retry_after is not None:
        result_json["parameters"] = {"retry_after": retry_after}
    return telebot.apihelper.ApiTelegramException("sendPhoto", Mock(), result_json)


def test_should_pause_for_network_server_and_flood_errors():
    assert should_pause_for_telegram_error(requests_exceptions.ConnectionError("reset"))
    assert should_pause_for_telegram_error(_api_error(502, "Bad Gateway"))
    assert should_pause_for_telegram_error(_api_error(429, "Too Many Requests: retry after 5", 5))


def test_should_not_pause_for_definitive_rejections():
    assert not should_pause_for_telegram_error(_api_error(400, "Bad Request: wrong file identifier"))
    assert not should_pause_for_telegram_error(ValueError("bad payload"))


def test_retry_after_and_description():
    assert telegram_retry_after_seconds(_api_error(429, "Too Many Requests", 17)) == 17
    assert telegram_retry_after_seconds(_api_error(400, "Bad Request")) is None
    assert describe_telegram_error(_api_error(400, "Bad Request")) == "ApiTelegramException 400: Bad Request"


def test_search_treats_flood_control_as_transient_not_as_url_rejection():
    assert imageboard_search_bot._telegram_rejected_url(_api_error(400, "Bad Request: wrong file identifier"))
    assert not imageboard_search_bot._telegram_rejected_url(_api_error(429, "Too Many Requests: retry after 5", 5))


@pytest.mark.parametrize("module", [notifier_bot], ids=["notifier"])
def test_notifier_cooldown_honors_retry_after(module, monkeypatch):
    monkeypatch.setattr(module, "api_cooldown_until", 0)
    monkeypatch.setattr(shared.time, "time", lambda: 1000.0)

    module.pause_sending("test", _api_error(429, "Too Many Requests: retry after 9", 9))

    assert module.api_cooldown_until == 1000.0 + 9 + 2
