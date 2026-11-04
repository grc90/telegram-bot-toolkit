"""Tests for config_env QOL: validate_telegram_token."""

import pytest
from config_env import validate_telegram_token


VALID_TOKEN = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ123456789"


def test_valid_token_passes():
    # Should not raise
    validate_telegram_token("TEST_TOKEN", VALID_TOKEN)


def test_short_bot_id_passes():
    # Minimum 6-digit bot ID
    validate_telegram_token("TEST_TOKEN", "100000:ABCdefGHIjklMNOpqrSTUvwxYZ123456789")


def test_empty_token_raises():
    with pytest.raises(RuntimeError, match="does not look like a valid Telegram token"):
        validate_telegram_token("TEST_TOKEN", "")


def test_missing_colon_raises():
    with pytest.raises(RuntimeError, match="does not look like a valid Telegram token"):
        validate_telegram_token("TEST_TOKEN", "123456789ABCdefGHIjklMNOpqrSTUvwxYZ12345678")


def test_short_secret_raises():
    # Secret part must be 35+ chars
    with pytest.raises(RuntimeError, match="does not look like a valid Telegram token"):
        validate_telegram_token("TEST_TOKEN", "123456789:short")


def test_token_with_leading_space_raises():
    with pytest.raises(RuntimeError, match="does not look like a valid Telegram token"):
        validate_telegram_token("TEST_TOKEN", " " + VALID_TOKEN)


def test_token_with_trailing_newline_raises():
    with pytest.raises(RuntimeError, match="does not look like a valid Telegram token"):
        validate_telegram_token("TEST_TOKEN", VALID_TOKEN + "\n")


def test_alphabetic_bot_id_raises():
    # Bot ID must be numeric
    with pytest.raises(RuntimeError, match="does not look like a valid Telegram token"):
        validate_telegram_token("TEST_TOKEN", "botname:ABCdefGHIjklMNOpqrSTUvwxYZ12345678")


def test_error_message_includes_var_name():
    with pytest.raises(RuntimeError, match="MY_VAR"):
        validate_telegram_token("MY_VAR", "bad_token")


def test_get_int_env_reads_override_and_falls_back(monkeypatch):
    from config_env import get_int_env

    monkeypatch.setenv("TEST_INT_OVERRIDE", "42")
    monkeypatch.setenv("TEST_INT_EMPTY", "  ")
    monkeypatch.delenv("TEST_INT_MISSING", raising=False)

    assert get_int_env("TEST_INT_OVERRIDE", 7) == 42
    assert get_int_env("TEST_INT_EMPTY", 7) == 7
    assert get_int_env("TEST_INT_MISSING", 7) == 7


def test_get_int_env_rejects_non_integer(monkeypatch):
    import pytest

    from config_env import get_int_env

    monkeypatch.setenv("TEST_INT_BAD", "ten")

    with pytest.raises(RuntimeError, match="TEST_INT_BAD"):
        get_int_env("TEST_INT_BAD", 7)
