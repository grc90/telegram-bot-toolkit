import json

import pytest

from json_storage import JsonTypeError, MalformedJsonError, read_json_file, write_json_atomic


def test_write_json_atomic_round_trips(tmp_path):
    target = tmp_path / "state.json"

    write_json_atomic(target, {"items": [1, 2, 3]})

    assert json.loads(target.read_text(encoding="utf-8")) == {"items": [1, 2, 3]}


def test_read_json_file_missing_returns_default(tmp_path):
    assert read_json_file(tmp_path / "missing.json", default=[], expected_type=list) == []


def test_read_json_file_malformed_raises_without_deleting(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("{bad", encoding="utf-8")

    with pytest.raises(MalformedJsonError):
        read_json_file(target, default={}, expected_type=dict)

    assert target.read_text(encoding="utf-8") == "{bad"


def test_read_json_file_wrong_shape_raises(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("[]", encoding="utf-8")

    with pytest.raises(JsonTypeError):
        read_json_file(target, default={}, expected_type=dict)
