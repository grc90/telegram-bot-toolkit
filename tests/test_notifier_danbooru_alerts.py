"""Tests for the notifiers' operator alert when Danbooru keeps failing."""

import io
import urllib.error

import pytest

from imageboard_notifier_bot import APP as notifier_bot
import danbooru_notifier_shared as shared


NOTIFIERS = [pytest.param(notifier_bot, id="notifier")]


@pytest.fixture
def alerts(monkeypatch, tmp_path, request):
    module = request.param
    monkeypatch.setattr(module, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(module, "consecutive_danbooru_failures", 0)
    monkeypatch.setattr(module, "danbooru_failure_alerted", False)
    monkeypatch.setattr(module, "send_post_to_subscribers", lambda post: shared.SEND_RESULT_SENT)
    sent = []
    monkeypatch.setattr(module, "send_operator_alert", sent.append)
    return module, sent


def _fail_with_http(module, monkeypatch, code):
    def failing_fetch(updated_since=None):
        raise urllib.error.HTTPError("https://example.test", code, "error", {}, io.BytesIO())

    monkeypatch.setattr(module, "fetch_matching_posts", failing_fetch)


def _succeed(module, monkeypatch):
    monkeypatch.setattr(module, "fetch_matching_posts", lambda updated_since=None: [])


@pytest.mark.parametrize("alerts", NOTIFIERS, indirect=True)
def test_no_alert_below_threshold(alerts, monkeypatch):
    module, sent = alerts
    _fail_with_http(module, monkeypatch, 403)

    for _ in range(shared.DANBOORU_FAILURE_ALERT_THRESHOLD - 1):
        module.check_danbooru_once("test")

    assert sent == []
    assert module.consecutive_danbooru_failures == shared.DANBOORU_FAILURE_ALERT_THRESHOLD - 1


@pytest.mark.parametrize("alerts", NOTIFIERS, indirect=True)
def test_alerts_once_when_threshold_reached(alerts, monkeypatch):
    module, sent = alerts
    _fail_with_http(module, monkeypatch, 403)

    for _ in range(shared.DANBOORU_FAILURE_ALERT_THRESHOLD + 2):
        module.check_danbooru_once("test")

    assert len(sent) == 1
    assert "HTTP 403" in sent[0]


@pytest.mark.parametrize("alerts", NOTIFIERS, indirect=True)
def test_recovery_alert_after_alerted_outage(alerts, monkeypatch):
    module, sent = alerts
    _fail_with_http(module, monkeypatch, 503)

    for _ in range(shared.DANBOORU_FAILURE_ALERT_THRESHOLD):
        module.check_danbooru_once("test")

    _succeed(module, monkeypatch)
    module.check_danbooru_once("test")

    assert len(sent) == 2
    assert "volvio a responder" in sent[1]
    assert module.consecutive_danbooru_failures == 0


@pytest.mark.parametrize("alerts", NOTIFIERS, indirect=True)
def test_success_before_threshold_resets_silently(alerts, monkeypatch):
    module, sent = alerts
    _fail_with_http(module, monkeypatch, 500)
    module.check_danbooru_once("test")

    _succeed(module, monkeypatch)
    module.check_danbooru_once("test")

    assert sent == []
    assert module.consecutive_danbooru_failures == 0
