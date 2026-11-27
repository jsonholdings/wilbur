"""R1 (2.64.0): a turn whose tool call Ollama could not parse is retried once
in constrained mode, which leaves `tools` out of the payload so the server's
XML tool-call parser never runs ("ollama: XML syntax error on line 72: element
<function> closed by </parameter>", LIVE-CHECKS-2026-11-26), then falls back
to the 2.62.0 note retry. tool_call_mode = "native" keeps the 2.62.0 policy.
"""
import io
import json

import pytest

import wilbur.llm as llm
from wilbur.config import Config
from conftest import say

XML_ERR = "ollama: XML syntax error on line 72: element <function> closed by </parameter>"
TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]


def _flaky(agent, client, errors):
    real = client.chat
    pending = list(errors)
    calls = []

    def chat(messages, tools=None, **kw):
        calls.append(dict(kw))
        if pending:
            raise RuntimeError(pending.pop(0))
        return real(messages, tools, **kw)
    agent.client.chat = chat
    return calls


def test_auto_retries_the_same_turn_constrained(make_agent):
    agent, client = make_agent([say("all done")])
    calls = _flaky(agent, client, [XML_ERR])
    events = []
    agent.on_event = lambda kind, data: events.append((kind, data))
    assert agent.run("go") == "all done"
    assert [c.get("constrained") for c in calls] == [None, True]
    assert [d["mode"] for k, d in events if k == "tool_parse_retry"] == ["constrained"]
    # No harness note: the constrained retry resends the same request.
    assert not any("could not parse" in str(m.get("content")) for m in client.seen[-1])


def test_clean_turn_is_never_constrained(make_agent):  # control
    agent, client = make_agent([say("ok")])
    calls = _flaky(agent, client, [])
    assert agent.run("go") == "ok"
    assert all("constrained" not in c for c in calls)


def test_second_failure_falls_back_to_the_note(make_agent):
    agent, client = make_agent([say("done")])
    calls = _flaky(agent, client, [XML_ERR, XML_ERR])
    assert agent.run("go") == "done"
    assert [c.get("constrained") for c in calls] == [None, True, None]
    assert "could not parse your last tool call" in client.seen[-1][-1]["content"]


def test_native_mode_never_constrains(make_agent):
    agent, client = make_agent([say("done")], tool_call_mode="native")
    calls = _flaky(agent, client, [XML_ERR])
    assert agent.run("go") == "done"
    assert all("constrained" not in c for c in calls)


def test_config_rejects_unknown_mode():
    config = Config()
    config.tool_call_mode = "xml"
    with pytest.raises(ValueError, match="tool_call_mode"):
        config.validate()


def _urlopen(reply, sent):
    def urlopen(request, timeout=None):
        sent.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"message": reply}).encode())
    return urlopen


def test_forced_constrained_payload_drops_tools(monkeypatch):
    sent = []
    content = json.dumps({"content": "", "tool_calls": [{"name": "read_file", "arguments": {"path": "a"}}]})
    monkeypatch.setattr(llm.urllib.request, "urlopen", _urlopen({"content": content}, sent))
    reply = llm.OllamaClient("http://x", "m").chat([], TOOLS, known_tools={"read_file"},
                                                   constrained=True)
    assert "tools" not in sent[0] and sent[0]["format"]["type"] == "object"
    assert [(c.name, c.arguments) for c in reply.tool_calls] == [("read_file", {"path": "a"})]


def test_default_payload_keeps_tools_and_no_format(monkeypatch):  # control
    sent = []
    monkeypatch.setattr(llm.urllib.request, "urlopen", _urlopen({"content": "ok"}, sent))
    llm.OllamaClient("http://x", "m").chat([], TOOLS, known_tools={"read_file"})
    assert sent[0]["tools"] == TOOLS and "format" not in sent[0]

