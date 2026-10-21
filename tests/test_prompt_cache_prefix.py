"""The prompt sent on turn N+1 must start with the prompt sent on turn N.

Ollama reuses its KV cache only for the byte-identical prompt prefix. On
2026-10-17 two consecutive turns re-read 48,813 and 49,402 tokens (~48 s
each) because the per-turn harness briefing was pinned right after the system
prompt and changed whenever the plan or failure ledger moved. 2.38.0 moved it to
the tail but re-sent it per call, so each prompt diverged at the old briefing
(qwen35 resumes only from end-of-prompt checkpoints: full re-reads). Since
2.40.1 the briefing is appended to the transcript only when it changes, so
every call's list is a strict prefix of the next call's list.
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


def _dumps(msgs):
    return [json.dumps(m, sort_keys=True) for m in msgs]


def _assert_append_only(sent):
    for prev, nxt in zip(sent, sent[1:]):
        assert _dumps(nxt[:len(prev)]) == _dumps(prev), "previous call is not a prefix"
        assert _rendered(nxt).startswith(_rendered(prev))


def test_each_prompt_extends_the_previous_one_byte_for_byte(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("x\n")
    a, client = _run(monkeypatch, tmp_path, [
        _todo("in_progress", "pending"),
        Reply(content="", tool_calls=[ToolCall(name="read_file", arguments={"path": "missing"})]),
        Reply(content="", tool_calls=[ToolCall(name="read_file", arguments={"path": "a.txt"})]),
        _todo("completed", "in_progress"),
        Reply(content="done", tool_calls=[]),
    ])
    a.run("do two things")
    sent = client.seen
    assert len(sent) == 5
    briefings = {m["content"] for s in sent for m in s if "[wilbur:briefing]" in m["content"]}
    assert len(briefings) >= 3, "control: the briefing must actually change between turns"
    _assert_append_only(sent)
    # An unchanged briefing is not appended twice in a row.
    for s in sent:
        idx = [i for i, m in enumerate(s) if "[wilbur:briefing]" in m["content"]]
        texts = [s[i]["content"] for i in idx]
        assert all(x != y for x, y in zip(texts, texts[1:]))
    # Every call carries the current briefing as its latest briefing.
    assert [m for m in sent[-1] if "[wilbur:briefing]" in m["content"]][-1]["content"] \
        == a.state.briefing()


def test_control_the_2400_remove_and_resend_design_fails_the_check():
    """The 2.40.0 shape: transcript + a per-call briefing that is dropped next call."""
    t = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    call1 = t + [{"role": "user", "content": "[wilbur:briefing] plan 0/2"}]
    t2 = t + [{"role": "assistant", "content": "ok"}]
    call2 = t2 + [{"role": "user", "content": "[wilbur:briefing] plan 1/2"}]
    try:
        _assert_append_only([call1, call2])
    except AssertionError:
        return
    raise AssertionError("control: the prefix check must reject the 2.40.0 design")


def test_compaction_keeps_the_latest_briefing(monkeypatch, tmp_path):
    a, client = _run(monkeypatch, tmp_path, [Reply(content="ok", tool_calls=[])])
    a.state.objective = "keep me"
    a.messages += [{"role": "user", "content": a.state.briefing()}]
    for i in range(12):
        a.messages += [{"role": "user", "content": f"u{i}"},
                       {"role": "assistant", "content": f"a{i}"}]
    from wilbur import context as ctx
    monkeypatch.setattr(agent_mod, "compact", lambda msgs, c, cfg: [msgs[0]] + msgs[-4:])
    a._maybe_compact(force=True)
    assert not any("[wilbur:briefing]" in m["content"] for m in a.messages)
    out = a._outgoing()
    assert out[-1]["content"] == a.state.briefing()
    assert a._outgoing() == out, "unchanged briefing must not be re-appended"
    assert ctx  # module import sanity


def test_a_restored_pre_238_session_drops_its_pinned_briefing(monkeypatch, tmp_path):
    a, client = _run(monkeypatch, tmp_path, [Reply(content="ok", tool_calls=[])])
    a.messages.append({"role": "system", "content": "[wilbur:briefing]\nstale"})
    a.run("hi")
    assert not any("stale" in m["content"] for m in client.seen[0])
    assert sum("[wilbur:briefing]" in m["content"] for m in client.seen[0]) == 1
