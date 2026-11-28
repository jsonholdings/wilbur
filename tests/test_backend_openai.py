"""backend "openai" (wilbur/backend_openai.py, ADR-0004 decision 3).

Every test talks to a local fake OpenAI-compatible server on 127.0.0.1 (an
ephemeral port, no network): it records each request and answers with a
scripted SSE stream, so the real urllib/threading path is exercised. A
control pins the Ollama request byte-for-byte so the default backend cannot
drift while this one is added.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur import llm
from wilbur.backend_openai import OpenAIClient, list_models, read_api_key, to_openai_messages
from wilbur.config import Config
from wilbur.fake_backend import BACKENDS, make_client
from wilbur.llm import ModelNotResponding, OllamaClient

TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "read",
                                           "parameters": {"type": "object", "properties": {
                                               "path": {"type": "string"}}}}}]


def sse(*chunks: Any, done: bool = True) -> bytes:
    body = b": keep-alive comment\n\n"
    for chunk in chunks:
        body += b"data: " + json.dumps(chunk).encode() + b"\n\n"
    if done:
        body += b"data: [DONE]\n\n"
    return body


def delta(**d: Any) -> dict:
    return {"choices": [{"index": 0, "delta": d, "finish_reason": None}]}


def finish(reason: str = "stop", usage: dict | None = None, timings: dict | None = None) -> dict:
    chunk: dict = {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}
    if usage is not None:
        chunk["usage"] = usage
    if timings is not None:
        chunk["timings"] = timings
    return chunk


class FakeOpenAIServer:
    """Scripted responses: each item is (status, body bytes) or "hang"."""

    def __init__(self) -> None:
        self.script: list[Any] = []
        self.requests: list[dict] = []
        self.release = threading.Event()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                owner.requests.append({"path": self.path, "headers": dict(self.headers)})
                body = json.dumps({"data": [{"id": "small.gguf"}, {"id": "other"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                owner.requests.append({"path": self.path, "headers": dict(self.headers),
                                       "body": json.loads(self.rfile.read(length))})
                item = owner.script.pop(0) if owner.script else (200, sse(finish()))
                if item == "hang":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.flush()
                    owner.release.wait(5)
                    return
                status, body = item
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


@pytest.fixture
def server():
    srv = FakeOpenAIServer()
    yield srv
    srv.close()
    # let handler threads finish so the leaked-thread guard stays quiet
    for t in list(threading.enumerate()):
        if t is not threading.current_thread() and t.name.startswith(("Thread-", "wilbur-stream")):
            t.join(5)


def client(server: FakeOpenAIServer, **kw: Any) -> OpenAIClient:
    c = OpenAIClient(server.url, "small.gguf", 30, sleep=lambda s: server.requests.append({"slept": s}), **kw)
    c.stall_timeout = 5.0
    return c


def test_streamed_text_reasoning_usage_and_timings(server):
    server.script.append((200, sse(
        delta(role="assistant"), delta(reasoning_content="hmm "), delta(reasoning_content="ok"),
        delta(content="Hello"), delta(content=", world"),
        finish("stop"),
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 7,
                                  "prompt_tokens_details": {"cached_tokens": 4}},
         "timings": {"prompt_n": 12, "prompt_ms": 30.5, "predicted_n": 7, "predicted_ms": 140.0}})))
    c = client(server)
    phases: list[str] = []
    pieces: list[str] = []
    c.on_progress = lambda phase, info: phases.append(phase)
    c.on_content = pieces.append
    reply = c.chat([{"role": "user", "content": "hi"}], num_ctx=8192)
    assert reply.content == "Hello, world"
    assert reply.thinking == "hmm ok"
    assert (reply.prompt_tokens, reply.eval_tokens) == (12, 7)
    assert not reply.truncated and not reply.thinking_cut and reply.tool_calls == []
    assert pieces == ["Hello", ", world"]
    assert phases == ["thinking", "writing", "done"]
    assert c.last_metrics == {"prompt_eval_count": 12, "eval_count": 7,
                              "prompt_eval_duration": 30_500_000, "eval_duration": 140_000_000,
                              "prompt_cached_tokens": 4}
    req = server.requests[0]
    assert req["path"] == "/v1/chat/completions"
    body = req["body"]
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["model"] == "small.gguf" and body["temperature"] == 0.15 and body["top_p"] == 0.9
    assert "num_ctx" not in json.dumps(body) and "options" not in body
    assert "tools" not in body and "response_format" not in body and "max_tokens" not in body
    assert "Authorization" not in req["headers"]


def test_tool_call_fragments_are_assembled(server):
    server.script.append((200, sse(
        delta(tool_calls=[{"index": 0, "id": "call_a", "type": "function",
                           "function": {"name": "read_file", "arguments": ""}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": '{"pa'}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": 'th": "x.py"}'}}]),
        delta(tool_calls=[{"index": 1, "id": "call_b", "function": {"name": "read_file",
                                                                   "arguments": '{"path": "y",}'}}]),
        finish("tool_calls", usage={"prompt_tokens": 50, "completion_tokens": 20}))))
    reply = client(server).chat([{"role": "user", "content": "read"}], TOOLS, known_tools={"read_file"})
    assert [(c.name, c.arguments, c.call_id, c.recovered) for c in reply.tool_calls] == [
        ("read_file", {"path": "x.py"}, "call_a", False),
        ("read_file", {"path": "y"}, "call_b", False)]   # trailing comma repaired
    assert server.requests[0]["body"]["tools"] == TOOLS
    assert not reply.truncated


def test_tool_call_written_as_text_is_recovered(server):
    server.script.append((200, sse(
        delta(content='{"name": "read_file", "arguments": {"path": "z"}}'), finish())))
    reply = client(server).chat([{"role": "user", "content": "x"}], TOOLS, known_tools={"read_file"})
    assert [(c.name, c.arguments, c.recovered) for c in reply.tool_calls] == [("read_file", {"path": "z"}, True)]


def test_length_finish_marks_truncated_and_max_tokens_sent(server):
    server.script.append((200, sse(delta(content="cut"), finish("length"))))
    c = client(server)
    c.num_predict = 64
    reply = c.chat([{"role": "user", "content": "x"}])
    assert reply.truncated
    assert server.requests[0]["body"]["max_tokens"] == 64
    assert (reply.prompt_tokens, reply.eval_tokens) == (0, 0)   # no usage sent -> 0, like null counts


def test_constrained_retry_uses_response_format_and_drops_tools(server):
    server.script.append((200, sse(delta(content=json.dumps(
        {"content": "ok", "tool_calls": [{"name": "read_file", "arguments": {"path": "a"}}]})), finish())))
    reply = client(server).chat([{"role": "user", "content": "x"}], TOOLS,
                                known_tools={"read_file"}, constrained=True)
    body = server.requests[0]["body"]
    assert "tools" not in body
    rf = body["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] == llm.tool_call_schema(TOOLS)
    assert [(c.name, c.arguments) for c in reply.tool_calls] == [("read_file", {"path": "a"})]
    assert reply.content == "ok"


def test_think_off_and_think_budget(server):
    server.script.append((200, sse(delta(content="a"), finish())))
    c = client(server)
    c.chat([{"role": "user", "content": "x"}], think=False)
    assert server.requests[0]["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    c.think_budget = 3
    server.script.append((200, sse(*[delta(reasoning_content="t") for _ in range(10)], finish())))
    reply = c.chat([{"role": "user", "content": "x"}])
    assert reply.thinking_cut and reply.eval_tokens == 4 and reply.content == ""
    assert "chat_template_kwargs" not in server.requests[1]["body"]


def test_5xx_is_retried_with_backoff_4xx_is_not(server):
    server.script += [(503, b"busy"), (200, sse(delta(content="up"), finish()))]
    reply = client(server).chat([{"role": "user", "content": "x"}])
    assert reply.content == "up"
    assert {"slept": 1.0} in server.requests
    server.script.append((400, b'{"error": "bad"}'))
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        client(server).chat([{"role": "user", "content": "x"}])
    assert sum(1 for r in server.requests if "slept" in r) == 1


def test_stream_error_object_raises(server):
    server.script.append((200, sse({"error": {"message": "context too long"}}, done=False)))
    with pytest.raises(RuntimeError, match="context too long"):
        client(server).chat([{"role": "user", "content": "x"}])


def test_silent_server_raises_model_not_responding(server):
    server.script.append("hang")
    c = client(server)
    c.stall_timeout = 0.3
    c.stall_busy_max_s = 0.0
    with pytest.raises(ModelNotResponding):
        c.chat([{"role": "user", "content": "x"}])


def test_api_key_file_sent_as_bearer_and_0644_refused(server, tmp_path):
    key = tmp_path / "key"
    key.write_text("sk-local-test\n")
    os.chmod(key, 0o600)
    assert read_api_key(str(key)) == "sk-local-test"
    assert read_api_key("") == ""
    bearer = read_api_key(str(key))
    c = client(server, api_key=bearer)
    c.chat([{"role": "user", "content": "x"}])
    assert server.requests[0]["headers"]["Authorization"] == "Bearer sk-local-test"
    os.chmod(key, 0o644)
    with pytest.raises(PermissionError):
        read_api_key(str(key))
    assert list_models(server.url) == ["small.gguf", "other"]


def test_history_conversion_pairs_tool_results():
    history = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "look", "images": ["QUJD"]},
        {"role": "assistant", "content": "", "thinking": "x", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a"}}},
            {"id": "given", "function": {"name": "read_file", "arguments": '{"path": "b"}'}}]},
        {"role": "tool", "tool_name": "read_file", "content": "A"},
        {"role": "tool", "tool_name": "read_file", "tool_call_id": "given", "content": "B"},
    ]
    out = to_openai_messages(history)
    assert out[1]["content"][1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}
    calls = out[2]["tool_calls"]
    assert calls[0] == {"id": "call_0", "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a"}'}}
    assert calls[1]["id"] == "given" and calls[1]["function"]["arguments"] == '{"path": "b"}'
    assert "thinking" not in out[2]
    assert out[3] == {"role": "tool", "content": "A", "tool_call_id": "call_0", "name": "read_file"}
    assert out[4]["tool_call_id"] == "given"


def test_make_client_selects_backend_and_ollama_stays_default(tmp_path):
    assert Config().backend == "ollama"
    assert "openai" in BACKENDS
    cfg = Config(backend="openai", base_url="http://127.0.0.1:1", num_predict=99,
                 think_budget_tokens=7)
    c = make_client(cfg, "m")
    assert type(c) is OpenAIClient and c.num_predict == 99 and c.think_budget == 7
    assert type(make_client(Config(), "m")) is OllamaClient


def test_control_ollama_request_is_unchanged(monkeypatch):
    """Control: the Ollama /api/chat request is exactly what it was before
    the openai backend existed (pinned at 25d624c)."""
    seen: list[Any] = []

    class Resp:
        lines = [json.dumps({"message": {"content": "hi"}, "done": True,
                             "prompt_eval_count": None, "eval_count": 3,
                             "done_reason": "stop"}).encode() + b"\n", b""]

        def readline(self):
            return self.lines.pop(0)

        def close(self):
            pass

        def fileno(self):
            raise OSError

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        return Resp()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    c = OllamaClient("http://h:11434", "q", 10)
    c.num_predict = 5
    reply = c.chat([{"role": "user", "content": "x"}], TOOLS, num_ctx=4096)
    req = seen[0]
    assert req.full_url == "http://h:11434/api/chat"
    assert req.headers == {"Content-type": "application/json"}
    assert req.data == json.dumps({
        "model": "q", "messages": [{"role": "user", "content": "x"}], "stream": True,
        "options": {"temperature": 0.15, "top_p": 0.9, "num_ctx": 4096, "num_predict": 5},
        "keep_alive": "30m", "tools": TOOLS}).encode()
    assert (reply.content, reply.prompt_tokens, reply.eval_tokens) == ("hi", 0, 3)
