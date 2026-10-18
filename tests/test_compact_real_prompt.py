"""Compaction triggers on the real prompt size Ollama reports (2.37.5).

A 2026-10-17 session stopped three times with `empty_reply`: Ollama reported
64,582 prompt tokens against num_ctx 65,536, but the chars/3.5 estimate (which
also ignored the tool schema sent every call) said 45,004 -- just under the
45,056 compaction budget -- so it never compacted. With ~950 tokens left the
model spent them thinking and returned empty content twice.
"""
from wilbur.llm import Reply

from conftest import say


def _history(agent, n=12, size=8000):
    # ~27k estimated tokens: under the 45,056 budget, but over the 30% recent
    # slice, so compaction really summarises something.
    for i in range(n):
        agent.messages.append({"role": "user" if i % 2 == 0 else "assistant",
                               "content": f"m{i} " + "x" * size})


def test_compacts_when_real_prompt_tokens_exceed_budget_but_estimate_does_not(make_agent):
    # Turn 1: a tool-free reply whose real prompt size is near the window.
    # Turn 2 (next run) must compact before calling the model again.
    agent, client = make_agent([
        Reply(content="first answer", tool_calls=[], prompt_tokens=64_582),
        Reply(content="summary notes", tool_calls=[]),   # the compaction call
        say("second answer"),
    ], num_ctx=65_536)
    _history(agent)
    events = []
    agent.on_event = lambda kind, data: events.append((kind, data))
    agent.run("first")
    assert not any(k == "compact" for k, _ in events)
    agent.run("second")
    compacts = [d for k, d in events if k == "compact"]
    assert compacts, "real prompt_tokens over budget must trigger compaction"
    assert compacts[0]["before_tokens"] >= 64_582
    # The model saw the compacted history on the second call.
    assert any("[Earlier conversation, compacted]" in (m.get("content") or "")
               for m in client.seen[-1])


def test_estimate_includes_tool_schema(make_agent):
    agent, _ = make_agent([])
    from wilbur.context import estimate_tokens
    assert agent._estimate_prompt() > estimate_tokens(agent.messages)


def test_empty_reply_near_full_window_compacts_and_retries(make_agent):
    agent, client = make_agent([
        Reply(content="", tool_calls=[], thinking="hmm", prompt_tokens=64_582),
        Reply(content="summary notes", tool_calls=[]),   # the compaction call
        say("done after compaction"),
    ], num_ctx=65_536)
    _history(agent)
    events = []
    agent.on_event = lambda kind, data: events.append((kind, data))
    final = agent.run("go")
    kinds = [k for k, _ in events]
    assert "compact" in kinds
    assert "empty_reply" not in kinds, "a context-full empty reply is not an empty reply"
    assert final == "done after compaction"
    assert agent.last_reply_empty is False
    # The empty assistant turn was dropped, not left in the transcript.
    assert not any(m.get("role") == "assistant" and m.get("content") == ""
                   for m in agent.messages)


def test_empty_reply_with_room_left_still_uses_the_empty_retry(make_agent):
    agent, _ = make_agent([
        Reply(content="", tool_calls=[], prompt_tokens=1_000),
        say("ok"),
    ], num_ctx=65_536)
    events = []
    agent.on_event = lambda kind, data: events.append((kind, data))
    assert agent.run("go") == "ok"
    kinds = [k for k, _ in events]
    assert "empty_reply" in kinds and "compact" not in kinds
