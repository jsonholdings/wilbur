"""llm-stream-total-cap: OllamaClient.timeout is an idle cap reset by every
chunk, not a cap on the whole reply; a malformed NDJSON line fails cleanly."""
from __future__ import annotations

import json
import threading
import time

import pytest

from wilbur import llm
from wilbur.llm import ModelNotResponding, OllamaClient, TransientOllamaError


class Stream:
    def __init__(self, lines: list[tuple[float, bytes]]) -> None:
        self.pending = lines  # (delay after previous line, raw bytes)
        self.closed = threading.Event()
        self.next_at = time.monotonic() + (lines[0][0] if lines else 0)

    def readline(self) -> bytes:
        while not self.closed.is_set():
            if not self.pending:
                return b""
            if time.monotonic() >= self.next_at:
                _, raw = self.pending.pop(0)
                if self.pending:
                    self.next_at = time.monotonic() + self.pending[0][0]
                return raw
            time.sleep(0.005)
        return b""

    def close(self) -> None:
        self.closed.set()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _line(chunk: dict) -> bytes:
    return json.dumps(chunk).encode() + b"\n"


def _client(monkeypatch, stream, *, idle_cap: float) -> OllamaClient:
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda request, timeout=None: stream)
    client = OllamaClient("http://x", "m", max_retries=0)
    client.stall_timeout = 0.05
    client.stall_busy_max_s = 5.0
    client.busy_check = lambda silent: True  # server says it is still generating
    client.timeout = idle_cap
    return client


def test_long_reply_that_keeps_streaming_is_not_cut(monkeypatch):
    # 15 chunks 0.05 s apart = ~0.75 s total, well past the 0.3 s cap.
    lines = [(0.05, _line({"message": {"content": "x"}, "done": False})) for _ in range(15)]
    lines.append((0.05, _line({"message": {"content": "!"}, "done": True})))
    client = _client(monkeypatch, Stream(lines), idle_cap=0.3)
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert reply.content == "x" * 15 + "!"


def test_control_real_silence_past_idle_cap_still_raises(monkeypatch):
    lines = [(0.0, _line({"message": {"content": "x"}, "done": False})),
             (2.0, _line({"message": {"content": "y"}, "done": True}))]
    client = _client(monkeypatch, Stream(lines), idle_cap=0.3)
    with pytest.raises(ModelNotResponding) as info:
        client.chat([{"role": "user", "content": "hi"}])
    assert info.value.seconds >= 0.3


def test_malformed_ndjson_line_is_a_clear_retryable_error(monkeypatch):
    lines = [(0.0, _line({"message": {"content": "x"}, "done": False})),
             (0.0, b'{"message": {"content": "tru\n')]
    client = _client(monkeypatch, Stream(lines), idle_cap=5)
    with pytest.raises(TransientOllamaError) as info:
        client.chat([{"role": "user", "content": "hi"}])
    assert "malformed stream line" in str(info.value)
    assert not isinstance(info.value, json.JSONDecodeError)
