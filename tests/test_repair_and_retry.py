"""Tool-call JSON repair (trailing commas, single quotes, args-as-string)
and Ollama retry/backoff on transient errors.

Each repair case has a control proving the *unrepaired* text really is
invalid JSON and really was rejected before the fix (see test_recovery.py's
`test_bare_json_is_recovered` for the already-valid case this must not
disturb).
"""
import json
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wilbur.llm import (
    OllamaClient,
    _is_transient,
    _lenient_json_loads,
    recover_tool_calls,
)

KNOWN = {"run_bash", "read_file", "edit_file"}


# --- JSON repair -----------------------------------------------------------

def test_control_trailing_comma_is_invalid_strict_json():
    with pytest.raises(json.JSONDecodeError):
        json.loads('{"name": "run_bash", "arguments": {"command": "ls",}}')


def test_trailing_comma_before_brace_is_repaired():
    content = '{"name": "run_bash", "arguments": {"command": "ls",}}'
    calls, rest = recover_tool_calls(content, KNOWN)
    assert [c.name for c in calls] == ["run_bash"]
    assert calls[0].arguments == {"command": "ls"}
    assert rest == ""


def test_trailing_comma_before_bracket_is_repaired():
    parsed = _lenient_json_loads('{"a": [1, 2, 3,], "b": 1,}')
    assert parsed == {"a": [1, 2, 3], "b": 1}


def test_control_single_quoted_is_invalid_strict_json():
    with pytest.raises(json.JSONDecodeError):
        json.loads("{'name': 'read_file', 'arguments': {'path': '/tmp/a'}}")


def test_single_quoted_call_is_repaired():
    content = "{'name': 'read_file', 'arguments': {'path': '/tmp/a'}}"
    calls, rest = recover_tool_calls(content, KNOWN)
    assert [c.name for c in calls] == ["read_file"]
    assert calls[0].arguments == {"path": "/tmp/a"}


def test_mixed_quotes_are_left_alone_not_mangled():
    # A double-quoted value that legitimately contains an apostrophe must
    # never be swapped -- swapping would corrupt the string, not repair it.
    content = '{"name": "run_bash", "arguments": {"command": "echo it'"'"'s ok"}}'
    calls, rest = recover_tool_calls(content, KNOWN)
    assert [c.name for c in calls] == ["run_bash"]
    assert calls[0].arguments["command"] == "echo it's ok"


def test_control_string_arguments_field_is_rejected_without_fix():
    # Bypasses recover_tool_calls' outer repair to prove _coerce_call's own
    # str-arguments path needs _lenient_json_loads, not plain json.loads.
    raw = json.loads('{"name": "read_file", "arguments": "{\\"path\\": \\"/tmp/a\\",}"}')
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw["arguments"])


def test_args_as_string_with_trailing_comma_is_repaired():
    content = '{"name": "read_file", "arguments": "{\\"path\\": \\"/tmp/a\\",}"}'
    calls, rest = recover_tool_calls(content, KNOWN)
    assert [c.name for c in calls] == ["read_file"]
    assert calls[0].arguments == {"path": "/tmp/a"}


def test_already_valid_json_is_unaffected_by_repair():
    content = '{"name": "run_bash", "arguments": {"command": "wc -l /etc/hostname"}}'
    calls, rest = recover_tool_calls(content, KNOWN)
    assert calls[0].arguments == {"command": "wc -l /etc/hostname"}


# --- retry / backoff ---------------------------------------------------------

def test_control_4xx_is_never_retried():
    exc = urllib.error.HTTPError("http://x", 404, "not found", {}, None)
    assert _is_transient(exc) is False


def test_5xx_is_transient():
    exc = urllib.error.HTTPError("http://x", 503, "unavailable", {}, None)
    assert _is_transient(exc) is True


def test_connection_reset_is_transient():
    assert _is_transient(ConnectionResetError()) is True


def test_timeout_is_transient():
    assert _is_transient(socket.timeout()) is True


def test_retry_succeeds_after_transient_failures_with_backoff(monkeypatch):
    sleeps: list[float] = []
    calls = {"n": 0}
    body = json.dumps({"message": {"content": "ok"}, "prompt_eval_count": 1,
                        "eval_count": 1}).encode()

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionResetError("reset")
        return _FakeResponse()

    import wilbur.llm as llm_mod
    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    client = OllamaClient("http://x", "m", max_retries=3, backoff_base=0.01,
                           sleep=sleeps.append)
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert reply.content == "ok"
    assert calls["n"] == 3
    assert sleeps == [0.01, 0.02]


def test_retry_is_bounded_and_gives_up(monkeypatch):
    sleeps: list[float] = []

    def always_fails(request):
        raise ConnectionResetError("reset")

    client = OllamaClient("http://x", "m", max_retries=2, backoff_base=0.01,
                           sleep=sleeps.append)

    def fake_urlopen(request, timeout=None):
        raise ConnectionResetError("reset")

    import wilbur.llm as llm_mod
    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ConnectionResetError):
        client.chat([{"role": "user", "content": "hi"}])
    # 2 retries after the first attempt -> 2 sleeps, 3 attempts total.
    assert len(sleeps) == 2
    assert sleeps == [0.01, 0.02]


def test_4xx_is_not_retried_at_all(monkeypatch):
    sleeps: list[float] = []
    attempts = {"n": 0}

    def fake_urlopen(request, timeout=None):
        attempts["n"] += 1
        raise urllib.error.HTTPError("http://x", 400, "bad request", {}, None)

    import wilbur.llm as llm_mod
    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    client = OllamaClient("http://x", "m", max_retries=3, backoff_base=0.01,
                           sleep=sleeps.append)
    with pytest.raises(urllib.error.HTTPError):
        client.chat([{"role": "user", "content": "hi"}])
    assert attempts["n"] == 1
    assert sleeps == []
