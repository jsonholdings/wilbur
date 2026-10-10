"""ModelInfo properties and ModelManager.agent_capable().

ModelInfo is built directly (no Ollama server, no network, no GPU), and
agent_capable() is exercised with list_models monkeypatched to a fixed list
so no HTTP call is ever made.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur.models import ModelInfo, ModelManager


def test_size_gb_is_bytes_over_one_billion():
    info = ModelInfo(name="m", size_bytes=2_200_000_000)
    assert info.size_gb == 2.2


def test_size_gb_zero_for_zero_bytes():
    assert ModelInfo(name="m", size_bytes=0).size_gb == 0.0


def test_supports_tools_reflects_capabilities():
    with_tools = ModelInfo(name="a", size_bytes=0, capabilities=("tools", "vision"))
    without = ModelInfo(name="b", size_bytes=0, capabilities=("vision",))
    assert with_tools.supports_tools
    assert not without.supports_tools
    # Empty capabilities (the dataclass default) is not tool-capable.
    assert not ModelInfo(name="c", size_bytes=0).supports_tools


def test_supports_thinking_reflects_capabilities():
    thinking = ModelInfo(name="a", size_bytes=0, capabilities=("thinking",))
    plain = ModelInfo(name="b", size_bytes=0, capabilities=("tools",))
    assert thinking.supports_thinking
    assert not plain.supports_thinking
    assert not ModelInfo(name="c", size_bytes=0).supports_thinking


def test_agent_capable_filters_to_tool_capable_models(monkeypatch):
    manager = ModelManager("http://localhost:11434")
    fixed = [
        ModelInfo(name="zeta", size_bytes=0, capabilities=("tools",)),
        ModelInfo(name="alpha", size_bytes=0, capabilities=("vision",)),
        ModelInfo(name="mid", size_bytes=0, capabilities=("tools", "thinking")),
    ]
    monkeypatch.setattr(manager, "list_models", lambda **kw: fixed)

    def no_http(*args, **kwargs):
        raise AssertionError("agent_capable() must not make an HTTP call")
    monkeypatch.setattr(manager, "_get", no_http)
    monkeypatch.setattr(manager, "_post", no_http)

    capable = manager.agent_capable()
    assert [m.name for m in capable] == ["zeta", "mid"]


def test_agent_capable_empty_when_nothing_supports_tools(monkeypatch):
    manager = ModelManager("http://localhost:11434")
    monkeypatch.setattr(manager, "list_models",
                        lambda **kw: [ModelInfo(name="a", size_bytes=0)])
    assert manager.agent_capable() == []
