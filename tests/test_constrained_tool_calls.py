import io
import json

import wilbur.llm as llm
from wilbur.config import Config
from wilbur.fake_backend import make_client

TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]


def _fake_urlopen(reply, sent):
    def urlopen(request, timeout=None):
        sent.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"message": reply}).encode())
    return urlopen


def test_schema_limits_names_to_offered_tools():
    schema = llm.tool_call_schema(TOOLS)
    assert schema["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == ["read_file"]


def test_constrained_reply_becomes_tool_call(monkeypatch):
    sent = []
    content = json.dumps({"content": "", "tool_calls": [{"name": "read_file", "arguments": {"path": "a"}}]})
    monkeypatch.setattr(llm.urllib.request, "urlopen", _fake_urlopen({"content": content}, sent))
    client = llm.OllamaClient("http://x", "m")
    client.constrained_tool_calls = True
    reply = client.chat([], TOOLS, known_tools={"read_file"})
    assert sent[0]["format"]["type"] == "object"
    assert [(c.name, c.arguments) for c in reply.tool_calls] == [("read_file", {"path": "a"})]
    assert reply.content == ""


def test_unknown_tool_name_dropped_and_text_kept(monkeypatch):
    content = json.dumps({"content": "hi", "tool_calls": [{"name": "rm_rf", "arguments": {}}]})
    monkeypatch.setattr(llm.urllib.request, "urlopen", _fake_urlopen({"content": content}, []))
    client = llm.OllamaClient("http://x", "m")
    client.constrained_tool_calls = True
    reply = client.chat([], TOOLS, known_tools={"read_file"})
    assert reply.tool_calls == [] and reply.content == "hi"


def test_model_ignoring_format_falls_back_to_recovery(monkeypatch):
    content = 'Sure. {"name": "read_file", "arguments": {"path": "b"}}'
    monkeypatch.setattr(llm.urllib.request, "urlopen", _fake_urlopen({"content": content}, []))
    client = llm.OllamaClient("http://x", "m")
    client.constrained_tool_calls = True
    reply = client.chat([], TOOLS, known_tools={"read_file"})
    assert reply.tool_calls[0].arguments == {"path": "b"}


def test_off_by_default_sends_no_format(monkeypatch):
    sent = []
    monkeypatch.setattr(llm.urllib.request, "urlopen", _fake_urlopen({"content": "ok"}, sent))
    reply = llm.OllamaClient("http://x", "m").chat([], TOOLS, known_tools={"read_file"})
    assert "format" not in sent[0] and reply.content == "ok"


def test_make_client_applies_config_flag():
    cfg = Config()
    cfg.constrained_tool_calls = True
    assert make_client(cfg, "m").constrained_tool_calls is True
    assert make_client(Config(), "m").constrained_tool_calls is False
