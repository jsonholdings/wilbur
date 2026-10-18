"""The prompt sent on turn N+1 must start with the prompt sent on turn N.

Ollama reuses its KV cache only for the byte-identical prompt prefix. On
2026-10-17 two consecutive turns re-read 48,813 and 49,402 tokens (~48 s
each) because the per-turn harness briefing was pinned right after the system
prompt and changed whenever the plan or failure ledger moved. Since 2.38.0 the
briefing rides as the last message and is never stored.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.agent as agent_mod
from wilbur.agent import Agent
from wilbur.config import Config
from wilbur.fake_backend import FakeClient
from wilbur.llm import Reply, ToolCall
from wilbur.tools.base import Approval


def _todo(status_a, status_b):
    return Reply(content="", tool_calls=[ToolCall(name="todo_write", arguments={"todos": [
        {"task": "first", "status": status_a}, {"task": "second", "status": status_b}]})])


def _rendered(messages):
    """What Ollama's template sees: every system message collated into one
    block at the top (template.collate), the rest in order."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [json.dumps(m, sort_keys=True) for m in messages if m["role"] != "system"]
    return system + "\x00" + "\x00".join(rest)


def _run(monkeypatch, tmp_path, replies):
    client = FakeClient(replies)
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    config = Config()
    config.max_turns = 10
    a = Agent(config, str(tmp_path), approve=lambda *_: Approval.GRANTED)
    return a, client


def test_each_prompt_extends_the_previous_one_byte_for_byte(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("x\n")
    a, client = _run(monkeypatch, tmp_path, [
        _todo("in_progress", "pending"),
        Reply(content="", tool_calls=[ToolCall(name="read_file", arguments={"path": "missing"})]),
        _todo("completed", "in_progress"),
        Reply(content="done", tool_calls=[]),
    ])
    a.run("do two things")
    sent = client.seen
    assert len(sent) == 4
    briefings = {m["content"] for s in sent for m in s if "[wilbur:briefing]" in m["content"]}
    assert len(briefings) >= 3, "control: the briefing must actually change between turns"
    for prev, nxt in zip(sent, sent[1:]):
        prev_stable = prev[:-1]  # the trailing briefing is the only per-call part
        assert [json.dumps(m, sort_keys=True) for m in nxt[:len(prev_stable)]] == \
            [json.dumps(m, sort_keys=True) for m in prev_stable]
        # And as Ollama renders it (system messages collated to the top).
        assert _rendered(nxt).startswith(_rendered(prev_stable))


def test_a_restored_pre_238_session_drops_its_pinned_briefing(monkeypatch, tmp_path):
    a, client = _run(monkeypatch, tmp_path, [Reply(content="ok", tool_calls=[])])
    a.messages.append({"role": "system", "content": "[wilbur:briefing]\nstale"})
    a.run("hi")
    assert not any("stale" in m["content"] for m in client.seen[0])
    assert sum("[wilbur:briefing]" in m["content"] for m in client.seen[0]) == 1
