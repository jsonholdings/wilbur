"""Transient-error classification in wilbur/llm.py.

`_is_transient()` decides whether `OllamaClient._post` retries a failed
request: 5xx responses and network-level failures (URLError, timeouts,
connection resets) are retryable; 4xx and anything else is not. These tests
pin down that classification against the exception types the function
actually inspects, with no network and no GPU involved.
"""
from __future__ import annotations

import io
import socket
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wilbur.llm import TransientOllamaError, _is_transient


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://127.0.0.1:11434/api/chat", code, "error", {}, io.BytesIO(b""))


def test_transient_error_types_are_transient():
    """Every exception type _is_transient() special-cases must classify True."""
    assert _is_transient(_http_error(500))
    assert _is_transient(_http_error(502))
    assert _is_transient(_http_error(503))
    assert _is_transient(_http_error(599))
    assert _is_transient(urllib.error.URLError(socket.timeout("timed out")))
    assert _is_transient(socket.timeout("timed out"))
    assert _is_transient(TimeoutError("timed out"))
    assert _is_transient(ConnectionError("reset"))
    assert _is_transient(ConnectionResetError("peer reset"))


def test_non_transient_error_types_are_not_transient():
    """4xx responses and plain exceptions must classify False."""
    assert not _is_transient(_http_error(400))
    assert not _is_transient(_http_error(404))
    assert not _is_transient(_http_error(499))
    assert not _is_transient(ValueError("bad request"))
    assert not _is_transient(KeyError("model"))


def test_transient_ollama_error_is_an_exception_subclass():
    assert issubclass(TransientOllamaError, Exception)
    err = TransientOllamaError("connection reset")
    assert isinstance(err, Exception)
    assert str(err) == "connection reset"
