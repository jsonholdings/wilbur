"""2.41.1 busy-aware stall handling and the per-reply output cap.

Ollama sends no bytes while the model writes a tool call (measured
2026-10-21: Wilbur's socket idle 109 s while the server generated 2,000+
tokens), so a silence past the stall window is only a dead server when the
server says it has stopped."""
from __future__ import annotations

import json
import threading
import time

import pytest

from wilbur import llm
from wilbur.config import Config
from wilbur.fake_backend import make_client
from wilbur.llm import ModelNotResponding, OllamaClient, Reply, ServerActivity, keep_alive_seconds

from conftest import say


def _line(chunk: dict) -> bytes:
    return json.dumps(chunk).encode() + b"\n"


class ScriptedStream:
    """A response whose readline() blocks until the test feeds it a line, the
    way a real socket blocks while Ollama buffers a tool call."""

    def __init__(self) -> None:
        self.pending: list[tuple[float, bytes]] = []
        self.closed = threading.Event()

    def feed(self, *chunks: dict, delay: float = 0.0) -> None:
        ready = time.monotonic() + delay
        self.pending += [(ready, _line(c)) for c in chunks]

    def readline(self) -> bytes:
        while not self.closed.is_set():
            if self.pending and self.pending[0][0] <= time.monotonic():
                return self.pending.pop(0)[1]
            time.sleep(0.01)
        return b""

    def close(self) -> None:
        self.closed.set()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _client(monkeypatch, stream, busy, *, window=0.05, cap=600.0):
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda request, timeout=None: stream)
    client = OllamaClient("http://x", "m")
    client.stall_timeout = window
    client.stall_busy_max_s = cap
    client.busy_check = busy
    client.timeout = 5  # hard deadline: a broken busy-cap can never hang the suite
    return client


def test_a_long_silent_but_busy_stream_completes(monkeypatch):
    stream = ScriptedStream()
    stream.feed({"message": {"thinking": "plan"}, "done": False})
    done = {"message": {"content": "", "tool_calls": [
        {"function": {"name": "write_file", "arguments": {"path": "a", "content": "x"}}}]},
        "done": True, "prompt_eval_count": 10, "eval_count": 9000}
    stream.feed(done, delay=0.5)   # ten stall windows of silence
    asked = []
    client = _client(monkeypatch, stream, lambda silent: asked.append(silent) or True)
    phases = []
    client.on_progress = lambda phase, info: phases.append((phase, info))
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert [c.name for c in reply.tool_calls] == ["write_file"]
    assert len(asked) >= 3, "the server was asked each time the window passed"
    assert any(p == "busy" for p, _ in phases)
    assert phases[-1][0] == "done"


def test_a_dead_server_still_raises_at_the_window(monkeypatch):
    stream = ScriptedStream()
    stream.feed({"message": {"content": "par"}, "done": False})
    client = _client(monkeypatch, stream, lambda silent: False, window=0.1)
    began = time.monotonic()
    with pytest.raises(ModelNotResponding):
        client.chat([{"role": "user", "content": "hi"}])
    assert time.monotonic() - began < 1.0
    assert stream.closed.is_set(), "the reader was unblocked, not left running"


def test_no_probe_keeps_the_old_raise_at_the_window(monkeypatch):
    stream = ScriptedStream()
    client = _client(monkeypatch, stream, None, window=0.1)
    with pytest.raises(ModelNotResponding):
        client.chat([{"role": "user", "content": "hi"}])


def test_a_broken_probe_does_not_hide_a_stall(monkeypatch):
    def broken(silent):
        raise OSError("probe down")
    client = _client(monkeypatch, ScriptedStream(), broken, window=0.1)
    with pytest.raises(ModelNotResponding):
        client.chat([{"role": "user", "content": "hi"}])


def test_busy_forever_stops_at_the_hard_cap(monkeypatch):
    client = _client(monkeypatch, ScriptedStream(), lambda silent: True, window=0.05, cap=0.3)
    began = time.monotonic()
    with pytest.raises(ModelNotResponding):
        client.chat([{"role": "user", "content": "hi"}])
    assert 0.25 <= time.monotonic() - began < 1.5


def test_the_output_cap_is_sent_and_a_length_stop_is_flagged(monkeypatch):
    sent = []
    stream = ScriptedStream()
    stream.feed({"message": {"content": "aaaa"}, "done": True, "done_reason": "length",
                 "eval_count": 64})

    def urlopen(request, timeout=None):
        sent.append(json.loads(request.data))
        return stream

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    client = OllamaClient("http://x", "m")
    client.num_predict = 64
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert sent[0]["options"]["num_predict"] == 64
    assert reply.truncated is True


def test_no_cap_sends_no_num_predict(monkeypatch):
    sent = []
    stream = ScriptedStream()
    stream.feed({"message": {"content": "ok"}, "done": True, "done_reason": "stop"})
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda request, timeout=None: sent.append(json.loads(request.data)) or stream)
    reply = OllamaClient("http://x", "m").chat([{"role": "user", "content": "hi"}])
    assert "num_predict" not in sent[0]["options"] and reply.truncated is False


def test_a_runaway_reply_at_the_cap_is_nudged_once_then_ends_clearly(make_agent):
    runaway = Reply(content="word " * 50, tool_calls=[], eval_tokens=32768, truncated=True)
    agent, client = make_agent([runaway, runaway])
    events = []
    agent.on_event = lambda kind, data: events.append((kind, data))
    final = agent.run("write the whole thing")
    caps = [d for k, d in events if k == "output_cap"]
    assert len(caps) == 2 and caps[0]["nudged"] and not caps[1]["nudged"]
    assert "cut off" in client.seen[1][-1]["content"], "the nudge reached the model"
    assert "output cap" in final


def test_a_capped_reply_that_recovers_carries_on(make_agent):
    runaway = Reply(content="x", tool_calls=[], eval_tokens=10, truncated=True)
    agent, _ = make_agent([runaway, say("done in parts")])
    assert agent.run("go") == "done in parts"


def test_config_fields_reach_the_client():
    config = Config()
    assert (config.first_token_timeout_s, config.stall_busy_max_s,
            config.stall_busy_check, config.num_predict) == (120, 600, "auto", 32768)
    config.first_token_timeout_s, config.stall_busy_max_s, config.num_predict = 45, 900, 2048
    client = make_client(config, "m")
    assert (client.stall_timeout, client.stall_busy_max_s, client.num_predict) == (45.0, 900.0, 2048)
    assert client.busy_check is not None
    config.stall_busy_check = "off"
    assert make_client(config, "m").busy_check is None


class _PsResponse:
    def __init__(self, body, date):
        self._body = json.dumps(body).encode()
        self.headers = {"Date": date}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ps(monkeypatch, expires_at, date="Thu, 22 Oct 2026 02:30:00 GMT", name="m"):
    body = {"models": [{"name": name, "model": name, "expires_at": expires_at}]}
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda url, timeout=None: _PsResponse(body, date))


def test_ps_reports_busy_while_the_expiry_predates_the_silence(monkeypatch):
    # Server now 02:30:00Z; expiry set 02:27:00 (+30m) = 3 min ago; quiet 2 min.
    _ps(monkeypatch, "2026-10-21T22:57:00.123456789-04:00")
    assert ServerActivity("http://x", "m", "30m", "ps").check(120) is True


def test_ps_reports_stopped_once_the_expiry_was_refreshed(monkeypatch):
    # The request finished 30 s ago (expiry refreshed) though we heard nothing for 120 s.
    _ps(monkeypatch, "2026-10-21T22:59:30.5-04:00")
    assert ServerActivity("http://x", "m", "30m", "ps").check(120) is False


def test_ps_unloaded_model_is_stopped_and_unknown_keep_alive_cannot_tell(monkeypatch):
    _ps(monkeypatch, "2026-10-21T22:57:00-04:00", name="other")
    assert ServerActivity("http://x", "m", "30m", "ps").check(120) is False
    _ps(monkeypatch, "2318-01-01T00:00:00Z")
    assert ServerActivity("http://x", "m", "-1", "ps").check(120) is None


def test_ps_unreachable_cannot_tell(monkeypatch):
    def down(url, timeout=None):
        raise OSError("refused")
    monkeypatch.setattr(llm.urllib.request, "urlopen", down)
    assert ServerActivity("http://x", "m", "30m", "ps").check(120) is None


def test_gpu_probe_only_for_a_local_server(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda cmd: "/usr/bin/nvidia-smi")

    class Done:
        stdout = "87\n"
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: Done())
    assert ServerActivity("http://127.0.0.1:11434", "m", "30m", "gpu").check(120) is True
    assert ServerActivity("http://gpu-box:11434", "m", "30m", "gpu").check(120) is None
    Done.stdout = "3\n"
    assert ServerActivity("http://localhost:11434", "m", "30m", "gpu").check(120) is False


@pytest.mark.parametrize("text,seconds", [
    ("30m", 1800.0), ("1h30m", 5400.0), ("300", 300.0), ("45s", 45.0),
    ("-1", None), ("0", None), ("", None), ("soon", None)])
def test_keep_alive_seconds(text, seconds):
    assert keep_alive_seconds(text) == seconds


class _ChunkedServer:
    """A real HTTP/1.1 chunked NDJSON server on loopback: sends one chunk,
    stays silent for `gap` seconds (None = forever, until stopped), then done."""

    def __init__(self, gap):
        import http.server

        release = self.release = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def chunk(obj):
                    data = _line(obj)
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                    self.wfile.flush()
                try:
                    chunk({"message": {"content": "a"}, "done": False})
                    release.wait(gap)
                    if gap is None:
                        return
                    chunk({"message": {"content": "b"}, "done": True, "eval_count": 2})
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.mark.parametrize("busy,expect", [(True, "ab"), (False, None)])
def test_real_socket_busy_wait_and_abort(busy, expect):
    server = _ChunkedServer(gap=0.6 if busy else None)
    try:
        client = OllamaClient(server.url, "m")
        client.stall_timeout = 0.1
        client.busy_check = lambda silent: busy
        client.timeout = 5
        if expect:
            assert client.chat([{"role": "user", "content": "hi"}]).content == expect
        else:
            began = time.monotonic()
            with pytest.raises(ModelNotResponding):
                client.chat([{"role": "user", "content": "hi"}])
            assert time.monotonic() - began < 2.0
    finally:
        server.stop()
