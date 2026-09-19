"""First-run model picker (`wilbur.__main__._ensure_model`).

A public release ships a mainstream default (`qwen3:14b` in config.py) instead
of an abliterated model someone would have to already know to pull. These
tests drive every branch against a fake ModelManager -- no live Ollama, no
touching the owner's real ~/.config/wilbur/config.json.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import wilbur.__main__ as main_mod
from wilbur.config import Config
from wilbur.models import ModelInfo


def model(name: str, size_gb: float = 5.0, tools: bool = True) -> ModelInfo:
    caps = ("tools",) if tools else ()
    return ModelInfo(name=name, size_bytes=int(size_gb * 1e9), capabilities=caps)


class FakeManager:
    def __init__(self, base_url, reachable=True, models=None, reclaimable=0):
        self.base_url = base_url
        self._reachable = reachable
        self._models = models or []
        self._reclaimable = reclaimable

    def available(self):
        return self._reachable

    def list_models(self):
        return self._models

    def reclaimable_vram_mb(self):
        return self._reclaimable


@pytest.fixture
def patched_config_path(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    monkeypatch.setattr("wilbur.config.CONFIG_PATH", path)
    monkeypatch.setattr(main_mod, "CONFIG_PATH", path)
    return path


def use_manager(monkeypatch, **kw):
    monkeypatch.setattr(main_mod, "ModelManager",
                         lambda base_url: FakeManager(base_url, **kw))


def test_ollama_unreachable_prints_howto(monkeypatch, capsys):
    use_manager(monkeypatch, reachable=False)
    config = Config()
    rc = main_mod._ensure_model(config)
    assert rc == 1
    out = capsys.readouterr().out
    assert "ollama serve" in out


def test_configured_model_already_installed_is_a_noop(monkeypatch, capsys):
    use_manager(monkeypatch, models=[model(Config().model)])
    config = Config()
    before = config.model
    rc = main_mod._ensure_model(config)
    assert rc is None
    assert config.model == before
    assert capsys.readouterr().out == ""


def test_configured_but_non_tool_capable_triggers_picker(monkeypatch, capsys):
    """Installed by name, but without tool support -- must not be treated as usable."""
    config = Config()
    use_manager(monkeypatch, models=[model(config.model, tools=False), model("other:1b")])
    monkeypatch.setattr(main_mod, "gpu_free_mb", lambda: 24000)
    monkeypatch.setattr("builtins.input", lambda *_: "1")
    rc = main_mod._ensure_model(config)
    assert rc is None
    assert config.model == "other:1b"


@pytest.mark.parametrize("free_mb,expect", [
    (4000, "qwen2.5:7b"),
    (12000, "qwen3:14b"),
    (24000, "qwen3:32b"),
    (None, "qwen3:14b"),
])
def test_nothing_tool_capable_suggests_pull_sized_to_vram(monkeypatch, capsys, free_mb, expect):
    use_manager(monkeypatch, models=[model("no-tools:1b", tools=False)])
    monkeypatch.setattr(main_mod, "gpu_free_mb", lambda: free_mb)
    config = Config()
    rc = main_mod._ensure_model(config)
    assert rc == 1
    out = capsys.readouterr().out
    assert f"ollama pull {expect}" in out


def test_picks_interactively_and_saves(monkeypatch, capsys, patched_config_path):
    config = Config()
    a = model("aaa:7b")
    b = model("bbb:14b")
    use_manager(monkeypatch, models=[a, b], reclaimable=0)
    monkeypatch.setattr(main_mod, "gpu_free_mb", lambda: 24000)
    monkeypatch.setattr("builtins.input", lambda *_: "2")

    rc = main_mod._ensure_model(config)

    assert rc is None
    assert config.model == "bbb:14b"
    assert config.subagent_model == "bbb:14b"
    saved = Config.load()
    assert saved.model == "bbb:14b"
    out = capsys.readouterr().out
    assert "aaa:7b" in out and "bbb:14b" in out


def test_picker_reprompts_on_bad_input(monkeypatch, capsys, patched_config_path):
    config = Config()
    a = model("aaa:7b")
    use_manager(monkeypatch, models=[a])
    monkeypatch.setattr(main_mod, "gpu_free_mb", lambda: None)
    answers = iter(["nope", "9", "1"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))

    rc = main_mod._ensure_model(config)

    assert rc is None
    assert config.model == "aaa:7b"
