"""2.38.0: a long or stalled model call never looks frozen.

- the reply streams, and each phase (reading / thinking / writing) reaches the
  status line with its own elapsed seconds;
- silence past the stall window raises ModelNotResponding at once (no silent
  600 s wait, no retries) and the message offers /retry;
- /compact compacts on demand and prints tokens before -> after.
"""
import io
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import wilbur.agent as agent_mod
import wilbur.llm as llm
from wilbur import ui
from wilbur.agent import Agent
from wilbur.config import Config
from wilbur.fake_backend import FakeClient, make_client
from wilbur.llm import ModelNotResponding, OllamaClient, Reply
from wilbur.repl import Repl, _phase_text
from wilbur.tools.base import Approval


def _ndjson(*chunks):
    return io.BytesIO(b"".join(json.dumps(c).encode() + b"\n" for c in chunks))


def test_streamed_reply_reports_each_phase_and_folds_the_chunks(monkeypatch):
    sent = []

    def urlopen(request, timeout=None):
        sent.append((json.loads(request.data), timeout))
        return _ndjson(
            {"message": {"thinking": "hm"}, "done": False},
            {"message": {"thinking": "m"}, "done": False},
            {"message": {"content": "hel"}, "done": False},
            {"message": {"content": "lo", "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "a"}}}]}, "done": False},
            {"message": {"content": ""}, "done": True,
             "prompt_eval_count": 200, "eval_count": 7},
        )

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    client = OllamaClient("http://x", "m")
    phases = []
    client.on_progress = lambda phase, info: phases.append(phase)
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert sent[0][0]["stream"] is True
    # 2.41.1: the stall window is enforced by the caller's queue wait; the
    # socket timeout only bounds the reader thread (window + busy cap).
    assert sent[0][1] == 120.0 + 600.0
    assert phases == ["thinking", "writing", "done"]
    assert reply.content == "hello" and reply.thinking == "hmm"
    assert [c.name for c in reply.tool_calls] == ["read_file"]
    assert (reply.prompt_tokens, reply.eval_tokens) == (200, 7)


def test_no_first_byte_within_the_window_is_reported_not_retried(monkeypatch):
    sleeps, attempts = [], []

    def urlopen(request, timeout=None):
        attempts.append(timeout)
        raise socket.timeout("timed out")

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    client = OllamaClient("http://x", "m", sleep=sleeps.append)
    client.stall_timeout = 5
    with pytest.raises(ModelNotResponding) as err:
        client.chat([{"role": "user", "content": "hi"}])
    assert attempts == [5 + 600.0] and sleeps == []
    assert "not responding" in str(err.value) and "/retry" in str(err.value)


def test_silence_mid_stream_is_reported(monkeypatch):
    class Stalls:
        lines = [json.dumps({"message": {"content": "par"}, "done": False}).encode() + b"\n"]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def readline(self):
            if self.lines:
                return self.lines.pop()
            raise TimeoutError("read timed out")

    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda request, timeout=None: Stalls())
    with pytest.raises(ModelNotResponding):
        OllamaClient("http://x", "m").chat([{"role": "user", "content": "hi"}])


def test_config_sets_the_stall_window():
    config = Config()
    config.first_token_timeout_s = 33
    assert make_client(config, "m").stall_timeout == 33.0


def test_agent_announces_the_prompt_read_before_each_call(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_mod, "OllamaClient", FakeClient([Reply(content="ok", tool_calls=[])]))
    events = []
    a = Agent(Config(), str(tmp_path), approve=lambda *_: Approval.GRANTED,
              on_event=lambda kind, data: events.append((kind, data)))
    a.run("hi")
    kinds = [k for k, _ in events]
    assert kinds.index("model_phase") < kinds.index("assistant")
    phase = dict(events)["model_phase"]
    assert phase["phase"] == "reading" and phase["tokens"] > 0


def test_status_line_shows_the_phase_with_its_own_seconds():
    assert _phase_text({"phase": "reading", "tokens": 48813}) == "reading ~48,813 tokens"
    assert _phase_text({"phase": "writing"}) == "writing"
    assert _phase_text({"phase": "done"}) is None
    sp = ui.Spinner(render=False)
    sp._start = 1.0
    assert sp.status_text().startswith("Thinking...")
    sp.set_phase("reading ~48,813 tokens")
    assert sp.status_text().startswith("reading ~48,813 tokens 0s · turn ")
    sp.set_phase(None)
    assert sp.status_text().startswith("Thinking...")


def _repl(monkeypatch, tmp_path, replies):
    monkeypatch.setattr(agent_mod, "OllamaClient", FakeClient(replies))
    repl = Repl(Config(), str(tmp_path))
    repl.manager.available = lambda: True
    return repl


def test_compact_command_compacts_now_and_prints_before_and_after(monkeypatch, tmp_path, capsys):
    repl = _repl(monkeypatch, tmp_path, [Reply(content="SUMMARY of earlier work", tool_calls=[])])
    for i in range(30):
        repl.agent.messages.append({"role": "user", "content": f"request {i} " + "x" * 2000})
        repl.agent.messages.append({"role": "assistant", "content": f"answer {i} " + "y" * 2000})
    before = len(repl.agent.messages)
    repl._command("/compact")
    out = capsys.readouterr().out
    assert len(repl.agent.messages) < before
    assert "compacted context:" in out and "->" in out
    assert any("SUMMARY of earlier work" in (m.get("content") or "") for m in repl.agent.messages)


def test_compact_command_with_little_history_says_so(monkeypatch, tmp_path, capsys):
    repl = _repl(monkeypatch, tmp_path, [])
    repl._command("/compact")
    assert "nothing to compact yet" in capsys.readouterr().out


def test_new_commands_are_listed():
    from wilbur.repl import HELP, _SLASH_COMMANDS
    for cmd in ("/compact", "/retry"):
        assert cmd in HELP and cmd in _SLASH_COMMANDS


def test_a_null_prompt_eval_count_becomes_zero(monkeypatch):
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda request, timeout=None: _ndjson(
        {"message": {"content": "ok"}, "done": True,
         "prompt_eval_count": None, "eval_count": None}))
    reply = OllamaClient("http://x", "m").chat([{"role": "user", "content": "hi"}])
    assert (reply.prompt_tokens, reply.eval_tokens) == (0, 0)
