"""A round of parallel reads must not overflow the compaction budget (2.62.0).

Live W3 run, 2026-11-26 (2.61.0, Qwen3.6 35b-a3b, num_ctx 32768): the model
read all twelve ~4.7k-token modules in ONE reply. The round's results
(~56.8k tokens) were over the budget, so the next round compacted them to a
~3.7k summary before the model saw them, and it issued the same twelve reads
again: 7 compactions in 359 s, 96 reads, 0 edits. Reads past the budget in a
round are now held back with a note; the first read of a round always runs.
"""
from wilbur.llm import Reply, ToolCall

from conftest import say

NUM_CTX = 32_768  # budget = 0.75 * 32768 - 4096 = 20,480 tokens


def _modules(tmp_path, n=12, chars=16_000):
    for i in range(n):
        (tmp_path / f"mod{i:02d}.py").write_text(f"# module {i}\n" + ("x = 1\n" * (chars // 6)))
    return [f"mod{i:02d}.py" for i in range(n)]


def _read_all(names):
    return Reply(content="", tool_calls=[ToolCall(name="read_file", arguments={"path": p})
                                         for p in names])


def test_round_of_reads_stays_under_budget_and_is_not_compacted(make_agent, tmp_path):
    names = _modules(tmp_path)
    agent, client = make_agent([_read_all(names), say("done")], num_ctx=NUM_CTX)
    events = []
    agent.on_event = lambda kind, data: events.append((kind, data))
    agent.run("read them")
    kinds = [k for k, _ in events]
    assert "compact" not in kinds, "the round's reads were compacted away before the model saw them"
    deferred = [d for k, d in events if k == "tool_deferred"]
    assert 0 < len(deferred) < len(names)
    tool_msgs = [m for m in client.seen[-1] if m.get("role") == "tool"]
    assert len(tool_msgs) == len(names)  # every call still has a result
    held = [m for m in tool_msgs if m["content"].startswith("[Not shown:")]
    assert len(held) == len(deferred)
    assert "module 0" in tool_msgs[0]["content"]  # the first read always runs
    assert agent._prompt_size() <= agent._compact_budget()


def test_reads_that_fit_are_not_held_back(make_agent, tmp_path):
    names = _modules(tmp_path, n=3, chars=2_000)
    agent, client = make_agent([_read_all(names), say("done")], num_ctx=NUM_CTX)
    events = []
    agent.on_event = lambda kind, data: events.append((kind, data))
    agent.run("read them")
    assert not [k for k, _ in events if k == "tool_deferred"]


def test_a_single_oversized_read_still_runs(make_agent, tmp_path):
    names = _modules(tmp_path, n=1, chars=90_000)
    agent, client = make_agent([_read_all(names), say("done")], num_ctx=NUM_CTX)
    events = []
    agent.on_event = lambda kind, data: events.append((kind, data))
    agent.run("read it")
    assert not [k for k, _ in events if k == "tool_deferred"]
    assert "module 0" in [m for m in client.seen[-1] if m.get("role") == "tool"][0]["content"]
