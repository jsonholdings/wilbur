"""Ollama tool-call parse errors are retried, not a stop (2.62.0).

Live W3 run, 2026-11-26 (2.61.0, Qwen3.6 35b-a3b): a 5-item task list ended
after item 4 with "RuntimeError: ollama: XML syntax error on line 72: element
<function> closed by </parameter>" -- the server could not parse the tool
call the model wrote -- and the REPL went to STOPPED/error.
"""
import pytest

from conftest import say

XML_ERR = "ollama: XML syntax error on line 72: element <function> closed by </parameter>"


def _flaky(agent, client, errors):
    real = client.chat
    pending = list(errors)

    def chat(messages, tools=None, **kw):
        if pending:
            client.seen.append([dict(m) for m in messages])
            raise RuntimeError(pending.pop(0))
        return real(messages, tools, **kw)
    agent.client.chat = chat


def test_parse_error_is_retried_with_a_note(make_agent):
    agent, client = make_agent([say("all done")], tool_call_mode="native")
    _flaky(agent, client, [XML_ERR])
    events = []
    agent.on_event = lambda kind, data: events.append((kind, data))
    assert agent.run("go") == "all done"
    assert [d["attempt"] for k, d in events if k == "tool_parse_retry"] == [1]
    assert "could not parse your last tool call" in client.seen[-1][-1]["content"]


def test_ollama_json_tool_call_error_is_retried(make_agent):
    agent, client = make_agent([say("ok")])
    _flaky(agent, client, ["ollama: error parsing tool call: raw='{\"a\":' err=unexpected end of JSON input"])
    assert agent.run("go") == "ok"


def test_gives_up_after_the_retry_budget(make_agent):
    agent, client = make_agent([say("never")])
    _flaky(agent, client, [XML_ERR] * 3)
    with pytest.raises(RuntimeError, match="XML syntax error"):
        agent.run("go")


def test_other_server_errors_still_raise(make_agent):
    agent, client = make_agent([say("never")])
    _flaky(agent, client, ["ollama: model requires more system memory"])
    with pytest.raises(RuntimeError, match="system memory"):
        agent.run("go")
