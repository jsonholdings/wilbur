"""2.56.0 (parity G8, BACKLOG fallback-model-one-shot): `wilbur -p` picks its
model like the REPL and `wilbur loop` -- the single-card fallback when the
default does not fit the cards present -- and `-p -m X` is never swapped.
Fakes only: a fake nvidia-smi, a fake ModelManager; no GPU, no Ollama."""
from __future__ import annotations

import subprocess

import wilbur.__main__ as main_mod
from wilbur.config import Config
from wilbur.models import ModelInfo

BIG = "huihui_ai/Qwen3.6-abliterated:35b-a3b-q8_0"
SMALL = "huihui_ai/qwen3.8-abliterated:27b"
OTHER = "qwen3:14b"
INSTALLED = [ModelInfo(BIG, int(34.6e9), capabilities=("tools",)),
             ModelInfo(SMALL, int(15.3e9), capabilities=("tools",)),
             ModelInfo(OTHER, int(9.3e9), capabilities=("tools",))]
ONE_FREE = "24000, 24576\n"       # second 3090 missing
TWO_FREE = "24000, 24576\n24000, 24576\n"


class FakeManager:
    def __init__(self, base_url):
        pass

    def list_models(self, **kw):
        return list(INSTALLED)

    def reclaimable_vram_mb(self):
        return 0


def _setup(monkeypatch, smi_out, fallback=SMALL):
    cfg = Config(model=BIG, fallback_model=fallback, gpu_indices=[0, 1], num_ctx=32768)
    cfg.subagent_model = cfg.model
    monkeypatch.setattr(main_mod.Config, "load", classmethod(lambda cls, *a, **k: cfg))
    monkeypatch.setattr(main_mod, "ModelManager", FakeManager)
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, smi_out, ""))
    seen = {}

    def fake_oneshot(config, cwd, prompt, auto):
        seen["model"], seen["sub"] = config.model, config.subagent_model
        return 0
    monkeypatch.setattr(main_mod, "_oneshot", fake_oneshot)
    return seen


def test_oneshot_falls_back_when_second_card_missing(monkeypatch, tmp_path, capsys):
    seen = _setup(monkeypatch, ONE_FREE)
    assert main_mod.main(["-p", "hi", "-C", str(tmp_path)]) == 0
    assert seen == {"model": SMALL, "sub": SMALL}
    captured = capsys.readouterr()
    assert "fallback model" in captured.err and SMALL in captured.err
    assert "fallback" not in captured.out   # stdout stays the reply


def test_control_both_cards_present_keeps_default(monkeypatch, tmp_path, capsys):
    """Control: the same path keeps the default when it fits."""
    seen = _setup(monkeypatch, TWO_FREE)
    assert main_mod.main(["-p", "hi", "-C", str(tmp_path)]) == 0
    assert seen["model"] == BIG
    assert "fallback" not in capsys.readouterr().err


def test_explicit_model_is_never_swapped(monkeypatch, tmp_path, capsys):
    seen = _setup(monkeypatch, ONE_FREE)
    assert main_mod.main(["-p", "hi", "-m", BIG, "-C", str(tmp_path)]) == 0
    assert seen["model"] == BIG
    assert "fallback" not in capsys.readouterr().err


def test_no_fallback_configured_changes_nothing(monkeypatch, tmp_path, capsys):
    seen = _setup(monkeypatch, ONE_FREE, fallback="")
    assert main_mod.main(["-p", "hi", "-C", str(tmp_path)]) == 0
    assert seen["model"] == BIG
