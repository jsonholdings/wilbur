"""2.50.0 two-card default with a single-card fallback. Fakes only: a fake
nvidia-smi and ModelInfo sizes; no GPU, no Ollama, no gq."""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.config as config_mod
from wilbur import loop
from wilbur.config import Config
from wilbur.models import ModelInfo, choose_session_model, fit_report, model_need_gb

BIG = "huihui_ai/Qwen3.6-abliterated:35b-a3b-q8_0"
SMALL = "huihui_ai/qwen3.8-abliterated:27b"
INSTALLED = {
    BIG: ModelInfo(BIG, int(34.6e9), capabilities=("tools",)),
    SMALL: ModelInfo(SMALL, int(15.3e9), capabilities=("tools",)),
}
TWO_FREE = "24000, 24576\n24000, 24576\n"
ONE_FREE = "24000, 24576\n"
SECOND_BUSY = "24000, 24576\n2000, 24576\n"
BOTH_BUSY = "3000, 24576\n2000, 24576\n"


def smi(monkeypatch, stdout):
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout, ""))


def two_card(**kw):
    cfg = Config(model=BIG, fallback_model=SMALL, gpu_indices=[0, 1], num_ctx=32768, **kw)
    cfg.subagent_model = cfg.subagent_model or cfg.model  # as Config.load does
    return cfg


def test_control_fit_check_can_say_does_not_fit():
    """Control: the check the fallback relies on can produce a negative."""
    fits, why = fit_report(INSTALLED[BIG], 32768, 24000)
    assert fits is False and "needed" in why
    assert fit_report(INSTALLED[BIG], 32768, 48000)[0] is True


def test_both_cards_fit_keeps_big_model_and_lease_spans(monkeypatch):
    smi(monkeypatch, TWO_FREE)
    cfg = two_card()
    assert choose_session_model(cfg, INSTALLED) is None
    assert cfg.model == BIG and cfg.subagent_model == BIG
    extra = loop.lease_extra(cfg, model_need_gb(INSTALLED[cfg.model], cfg.num_ctx))
    assert extra["vram_gb"] > 24 and extra["needed_vram_gb"] == extra["vram_gb"]
    assert extra["submitter"] == "wilbur"


def test_second_card_missing_falls_back_to_one_card(monkeypatch):
    smi(monkeypatch, ONE_FREE)
    cfg = two_card()
    note = choose_session_model(cfg, INSTALLED)
    assert note and SMALL in note and BIG in note and "unknown" in note
    assert cfg.model == SMALL and cfg.subagent_model == SMALL
    extra = loop.lease_extra(cfg, model_need_gb(INSTALLED[cfg.model], cfg.num_ctx))
    assert 0 < extra["vram_gb"] <= 24


def test_second_card_busy_falls_back(monkeypatch):
    smi(monkeypatch, SECOND_BUSY)
    cfg = two_card()
    note = choose_session_model(cfg, INSTALLED)
    assert note and "does not fit" in note and cfg.model == SMALL


def test_explicit_subagent_model_is_kept(monkeypatch):
    smi(monkeypatch, ONE_FREE)
    cfg = two_card(subagent_model="other:7b")
    assert choose_session_model(cfg, INSTALLED)
    assert cfg.model == SMALL and cfg.subagent_model == "other:7b"


def test_neither_fits_no_silent_switch(monkeypatch):
    smi(monkeypatch, BOTH_BUSY)
    cfg = two_card()
    assert choose_session_model(cfg, INSTALLED) is None
    assert cfg.model == BIG


def test_no_gpu_at_all_no_switch(monkeypatch):
    smi(monkeypatch, "")
    cfg = two_card()
    assert choose_session_model(cfg, INSTALLED) is None
    assert cfg.model == BIG


def test_fallback_not_installed_no_switch(monkeypatch):
    smi(monkeypatch, ONE_FREE)
    cfg = two_card()
    assert choose_session_model(cfg, {BIG: INSTALLED[BIG]}) is None
    assert cfg.model == BIG


def test_fallback_empty_is_unchanged(monkeypatch):
    smi(monkeypatch, ONE_FREE)
    cfg = Config(model=BIG, gpu_indices=[0, 1])
    assert cfg.fallback_model == ""
    assert choose_session_model(cfg, INSTALLED) is None
    assert cfg.model == BIG


def test_save_keeps_configured_default(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    smi(monkeypatch, ONE_FREE)
    cfg = two_card()
    assert choose_session_model(cfg, INSTALLED)
    cfg.save()
    data = json.loads(path.read_text())
    assert data["model"] == BIG and data["subagent_model"] == BIG
    assert data["fallback_model"] == SMALL


def test_lease_extra_unknown_need_sends_no_size():
    assert loop.lease_extra(Config(), need_gb=0) == {"submitter": "wilbur"}


def test_ensure_model_prints_fallback_line(monkeypatch, capsys):
    import wilbur.__main__ as main_mod
    smi(monkeypatch, ONE_FREE)

    class FakeManager:
        def __init__(self, url):
            pass

        def available(self):
            return True

        def list_models(self):
            return list(INSTALLED.values())

        def reclaimable_vram_mb(self):
            return 0

    monkeypatch.setattr(main_mod, "ModelManager", FakeManager)
    monkeypatch.setattr(main_mod, "_warn_gq_bypass", lambda: None)
    cfg = two_card()
    assert main_mod._ensure_model(cfg) is None
    assert cfg.model == SMALL
    assert "fallback model" in capsys.readouterr().out
    cfg2 = two_card()
    assert main_mod._ensure_model(cfg2, allow_fallback=False) is None
    assert cfg2.model == BIG
