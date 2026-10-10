"""Unit tests for the two pure helpers in wilbur/llm.py that repair tool
calls a local model almost got right: `_lenient_json_loads` (strict-first
JSON parsing with trailing-comma and single-quote repair) and `_coerce_call`
(accepting the call shapes the recovery path can produce, rejecting the rest).

Both are pure functions -- no network, no GPU, no config -- so the tests
exercise them directly and assert their CURRENT behaviour as implemented in
wilbur/llm.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.llm import ToolCall, _coerce_call, _lenient_json_loads


# --- _lenient_json_loads ----------------------------------------------------


def test_valid_json_parses_strictly():
    """Already-valid JSON goes through the strict path, untouched."""
    assert _lenient_json_loads('{"name": "run_bash", "arguments": {"command": "wc -l"}}') == {
        "name": "run_bash",
        "arguments": {"command": "wc -l"},
    }
    # Non-object JSON is fine too -- the helper is a parser, not a call checker.
    assert _lenient_json_loads("[1, 2, 3]") == [1, 2, 3]
    assert _lenient_json_loads("42") == 42


def test_trailing_comma_is_repaired():
    """The observed small-model failure: a comma before the closing brace."""
    assert _lenient_json_loads('{"a": 1,}') == {"a": 1}
    # Trailing commas inside nested arrays are repaired the same way.
    assert _lenient_json_loads('{"a": [1, 2,], "b": {"c": 2,},}') == {
        "a": [1, 2],
        "b": {"c": 2},
    }


def test_single_quoted_json_is_repaired():
    """Single-quoted strings are accepted when the text has no double quotes."""
    assert _lenient_json_loads("{'name': 'read_file', 'path': '/tmp/x'}") == {
        "name": "read_file",
        "path": "/tmp/x",
    }


def test_mixed_quote_styles_are_not_mangled():
    """A double-quoted value containing an apostrophe must survive: the
    single-quote swap is only attempted when no double quotes exist at all."""
    text = '{"note": "it\'s fine", "a": 1}'
    assert _lenient_json_loads(text) == {"note": "it's fine", "a": 1}


def test_unparseable_text_raises_json_decode_error():
    """Plain prose is rejected, not guessed at."""
    with pytest.raises(json.JSONDecodeError):
        _lenient_json_loads("I think the command is ls -la, roughly.")
    # Structurally broken JSON (missing value) is rejected too.
    with pytest.raises(json.JSONDecodeError):
        _lenient_json_loads('{"a": 1, "b": }')
    # Truncated mid-generation, as in tests/test_malformed_tool_calls.py.
    with pytest.raises(json.JSONDecodeError):
        _lenient_json_loads('{"name": "run_bash", "arguments": {"command": "ls -la /tmp')


# --- _coerce_call -----------------------------------------------------------


def test_well_formed_call_object():
    call = _coerce_call({"name": "run_bash", "arguments": {"command": "wc -l /etc/hostname"}})
    assert call is not None
    assert isinstance(call, ToolCall)
    assert call.name == "run_bash"
    assert call.arguments == {"command": "wc -l /etc/hostname"}
    assert call.recovered is True


def test_openai_style_nesting_and_string_arguments():
    """Both the {"function": {...}} wrapper and JSON-string arguments are
    accepted, and the string arguments go through the lenient parser."""
    nested = _coerce_call({
        "function": {"name": "read_file", "arguments": '{"path": "/tmp/x"}'},
    })
    assert nested is not None
    assert nested.name == "read_file"
    assert nested.arguments == {"path": "/tmp/x"}

    # A trailing comma inside the string arguments is repaired too.
    slack = _coerce_call({"name": "grep", "arguments": '{"pattern": "x",}'})
    assert slack is not None
    assert slack.arguments == {"pattern": "x"}


def test_missing_arguments_defaults_to_empty_dict():
    call = _coerce_call({"name": "todo_write"})
    assert call is not None
    assert call.arguments == {}


def test_objects_it_returns_none_for():
    # Not a dict at all.
    assert _coerce_call(["name", "run_bash"]) is None
    assert _coerce_call("run_bash") is None
    assert _coerce_call(None) is None
    # No usable name (absent, empty, or not a string).
    assert _coerce_call({"arguments": {}}) is None
    assert _coerce_call({"name": ""}) is None
    assert _coerce_call({"name": 42}) is None
    # Arguments that are not a dict and not a parseable JSON string.
    assert _coerce_call({"name": "run_bash", "arguments": ["ls"]}) is None
    assert _coerce_call({"name": "run_bash", "arguments": "not json at all"}) is None
    # A "function" wrapper that is not itself a dict leaves no name behind.
    assert _coerce_call({"function": "run_bash"}) is None
