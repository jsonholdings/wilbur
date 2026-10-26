"""Reasoning budget and the thinking-off retry (2.43.0).

Measured 2026-10-26: three ~6-minute calls each spent the whole 32k output cap
reasoning on a 30k prompt and came back empty, and the second cap hit ended
the run. The client now cuts reasoning at a budget, and the agent asks the
identical request again with thinking off instead of stopping.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur import llm
from wilbur.config import Config
from wilbur.fake_backend import make_client
from wilbur.llm import OllamaClient, Reply
from conftest import say, call


def act(name, **args):
    return Reply(content="", tool_calls=[call(name, **args)])
from test_stall_busy import ScriptedStream


def _client(monkeypatch, chunks, sent):
    stream = ScriptedStream()
    for chunk in chunks:
        stream.feed(chunk)

    def urlopen(request, timeout=None):
        sent.append(json.loads(request.data))
        return stream

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    return OllamaClient("http://x", "m")


def _think(n):
    return [{"message": {"thinking": "t"}, "done": False} for _ in range(n)]


def test_reasoning_past_the_budget_is_cut(monkeypatch):
    sent = []
    chunks = _think(10) + [{"message": {"content": "late"}, "done": True,
                            "done_reason": "stop", "eval_count": 11}]
    client = _client(monkeypatch, chunks, sent)
    client.think_budget = 4
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert reply.thinking_cut is True and reply.truncated is False
    assert reply.content == "" and reply.thinking == "ttttt"
    assert reply.eval_tokens == 5
    assert "think" not in sent[0]


def test_reasoning_that_turns_into_an_answer_is_not_cut(monkeypatch):
    sent = []
    chunks = (_think(3) + [{"message": {"content": "answer"}, "done": False}] + _think(5)
              + [{"message": {}, "done": True, "done_reason": "stop", "eval_count": 9}])
    client = _client(monkeypatch, chunks, sent)
    client.think_budget = 4
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert reply.thinking_cut is False and reply.content == "answer"


def test_think_off_is_sent_and_lifts_the_budget(monkeypatch):
    sent = []
    chunks = _think(10) + [{"message": {"content": "ok"}, "done": True, "done_reason": "stop"}]
    client = _client(monkeypatch, chunks, sent)
    client.think_budget = 4
    reply = client.chat([{"role": "user", "content": "hi"}], think=False)
    assert sent[0]["think"] is False
    assert reply.thinking_cut is False and reply.content == "ok"


def test_config_budget_reaches_the_client():
    config = Config()
    assert config.think_budget_tokens == 8192
    config.think_budget_tokens = 1000
    assert make_client(config, "m").think_budget == 1000


def _events(agent):
    seen = []
    agent.on_event = lambda kind, data: seen.append((kind, data))
    return seen


def test_a_cut_reply_is_asked_again_with_thinking_off(make_agent):
    cut = Reply(content="", tool_calls=[], thinking="x" * 50, eval_tokens=8193,
                thinking_cut=True)
    agent, client = make_agent([cut, say("the answer")])
    seen = _events(agent)
    assert agent.run("go") == "the answer"
    assert client.requests[0]["options"].get("think") is None
    assert client.requests[1]["options"]["think"] is False
    assert client.seen[1] == client.seen[0], "identical request: no nudge, cached prefix"
    assert [d for k, d in seen if k == "think_off_retry"][0]["cut"] is True
    assert not any(m.get("role") == "assistant" and not m.get("content")
                   and not m.get("tool_calls") for m in agent.messages)


def test_a_reply_that_fills_the_output_cap_reasoning_does_not_end_the_run(make_agent):
    # The 2026-10-26 failure: twice in one run the reply spent the whole cap
    # reasoning. It used to end the run on the second hit.
    capped = Reply(content="", tool_calls=[], thinking="x" * 50, eval_tokens=32768,
                   truncated=True)
    agent, client = make_agent([
        capped, act("run_bash", command="echo one"),
        capped, act("run_bash", command="echo two"),
        say("both steps done"),
    ])
    seen = _events(agent)
    assert agent.run("go") == "both steps done"
    assert sum(1 for k, _ in seen if k == "think_off_retry") == 2
    assert not any(k == "output_cap" for k, _ in seen)
    assert [r["options"].get("think") for r in client.requests] == [None, False, None, False, None]


def test_think_off_is_tried_once_then_the_empty_reply_path_takes_over(make_agent):
    thought = Reply(content="", tool_calls=[], thinking="x" * 50)
    agent, client = make_agent([thought, thought, say("answer")], empty_reply_retries=1)
    seen = _events(agent)
    assert agent.run("go") == "answer"
    assert [k for k, _ in seen if k in ("think_off_retry", "empty_reply")] == [
        "think_off_retry", "empty_reply"]


def test_a_cut_long_answer_still_gets_the_output_cap_nudge(make_agent):
    # Real content cut at the cap is not an all-reasoning reply.
    runaway = Reply(content="word " * 50, tool_calls=[], eval_tokens=32768, truncated=True)
    agent, client = make_agent([runaway, say("done in parts")])
    seen = _events(agent)
    assert agent.run("go") == "done in parts"
    assert not any(k == "think_off_retry" for k, _ in seen)
    assert any(k == "output_cap" for k, _ in seen)
