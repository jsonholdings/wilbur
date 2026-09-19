"""Malformed tool calls through the full agent loop, not just the recovery
parser in isolation (see tests/test_recovery.py for that). `wilbur/llm.py`'s
real `OllamaClient.chat()` runs `recover_tool_calls()` over `content`
whenever the model's own `tool_calls` field comes back empty (llm.py:186-187)
-- these tests build the `Reply` the same way that client would, so the
`FakeClient` fixture exercises the same recovered-call path the Agent
actually runs, and prove the unrecoverable case terminates cleanly instead
of hanging.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wilbur.llm import Reply, recover_tool_calls

from conftest import say

KNOWN_TOOLS = {"read_file", "write_file", "edit_file", "run_bash", "grep", "glob", "todo_write"}


def _reply_from_raw_text(content: str) -> Reply:
    """Mirror OllamaClient.chat's own recovery step (llm.py:186-187): the
    model returned no structured tool_calls, so whatever the text recovers
    to is what the Agent actually receives."""
    calls, cleaned = recover_tool_calls(content, KNOWN_TOOLS)
    return Reply(content=cleaned, tool_calls=calls)


def test_bare_json_malformed_call_is_recovered_and_executed(make_agent, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\n")
    raw = '{"name": "read_file", "arguments": {"path": "%s"}}' % f
    a, client = make_agent([_reply_from_raw_text(raw), say("read it")])

    final = a.run("read a.txt")

    assert final == "read it"
    assert len(client.seen) == 2  # one turn for the recovered call, one for the reply
    # The recovered call actually ran the real tool (a real file read), not
    # a no-op -- the result the model would have seen is in the transcript.
    tool_result = a.messages[-2]
    assert tool_result["role"] == "tool"
    assert "hello" in tool_result["content"]


def test_tagged_malformed_call_is_recovered_and_executed(make_agent, tmp_path):
    f = tmp_path / "b.txt"
    f.write_text("world\n")
    raw = ('Sure, let me check.\n<tool_call>\n'
           '{"name": "read_file", "arguments": {"path": "%s"}}\n</tool_call>' % f)
    a, client = make_agent([_reply_from_raw_text(raw), say("done")])

    final = a.run("read b.txt")

    assert final == "done"
    assert a.messages[-2]["role"] == "tool"
    assert "world" in a.messages[-2]["content"]


def test_unrecoverable_malformed_content_ends_the_turn_without_hanging(make_agent):
    """Prose that merely contains JSON-shaped text but names no known tool
    must never be executed and must never stall the loop -- recover_tool_calls
    returns no calls, so the Agent has to treat it as the final answer."""
    garbage = 'I think the config is {"host": "localhost", "port": 5432} roughly.'
    reply = _reply_from_raw_text(garbage)
    assert reply.tool_calls == []  # sanity: this really is the unrecoverable case

    a, client = make_agent([reply])
    final = a.run("what's the config?")

    assert final == garbage
    assert len(client.seen) == 1  # a single turn -- no retry loop, no hang
    assert not any(m["role"] == "tool" for m in a.messages)


def test_truncated_unparseable_json_ends_the_turn_without_hanging(make_agent):
    """A cut-off tool call (truncated mid-generation) is not valid JSON at
    all; recover_tool_calls must fail closed rather than raise or loop."""
    truncated = '{"name": "run_bash", "arguments": {"command": "ls -la /tmp'
    calls, cleaned = recover_tool_calls(truncated, KNOWN_TOOLS)
    assert calls == []

    a, client = make_agent([Reply(content=cleaned, tool_calls=calls)])
    final = a.run("list files")

    assert final == cleaned
    assert len(client.seen) == 1
