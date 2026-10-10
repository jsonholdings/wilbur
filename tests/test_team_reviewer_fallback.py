"""reviewer_model VRAM fallback: checked against free VRAM, and the fallback note
is surfaced (result, loop row, REPL event) instead of being dropped.

Fake backend and a faked gpu_free_mb only; no live GPU or Ollama.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import wilbur.models as models_mod
from tests.test_team import (APPROVE, CODER_DONE, CODER_WRITE_MARKER, DENY, PLAN,
                             TESTER_DONE, TESTER_WRITE, _orch, repo)  # noqa: F401
from wilbur.config import Config

MAIN = "main-model:27b"
REVIEWER = "reviewer-model:8b"


def _config(reviewer=REVIEWER, min_free=8192):
    config = Config()
    config.model = MAIN
    config.reviewer_model = reviewer
    config.team_reviewer_min_free_mb = min_free
    return config


@pytest.fixture
def models_seen(fake_client_factory, monkeypatch):
    """Scripted run whose client records the model each Agent was built with."""
    def build(replies):
        client = fake_client_factory(replies)
        seen = []

        def construct(base_url, model, *a, **kw):
            seen.append(model)
            return client
        import wilbur.agent as agent_mod
        monkeypatch.setattr(agent_mod, "OllamaClient", construct)
        return seen
    return build


HAPPY = [PLAN, TESTER_WRITE, TESTER_DONE, CODER_WRITE_MARKER, CODER_DONE, APPROVE]


def test_reviewer_model_used_when_vram_fits(repo, models_seen, monkeypatch):
    monkeypatch.setattr(models_mod, "gpu_free_mb", lambda index=0: 16000)
    seen = models_seen(HAPPY)
    result = _orch(repo, _config()).run("add x", branch="fits")
    assert result.status == "merged"
    assert seen[-1] == REVIEWER
    assert "16000MiB free" in result.reviewer_model_note


def test_reviewer_falls_back_when_vram_short(repo, models_seen, monkeypatch):
    monkeypatch.setattr(models_mod, "gpu_free_mb", lambda index=0: 3000)
    seen = models_seen(HAPPY)
    events = []
    orch = _orch(repo, _config())
    orch.on_event = lambda kind, data: events.append((kind, data))
    result = orch.run("add x", branch="short")
    assert result.status == "merged"
    assert seen[-1] == MAIN
    assert "only 3000MiB free" in result.reviewer_model_note
    assert "using the main model" in result.reviewer_model_note
    assert ("team_reviewer_model", {"note": result.reviewer_model_note}) in events


def test_reviewer_falls_back_when_vram_unknown(repo, models_seen, monkeypatch):
    monkeypatch.setattr(models_mod, "gpu_free_mb", lambda index=0: None)
    seen = models_seen(HAPPY)
    result = _orch(repo, _config()).run("add x", branch="unknown")
    assert seen[-1] == MAIN
    assert "could not be checked" in result.reviewer_model_note


def test_no_reviewer_model_means_no_note(repo, models_seen, monkeypatch):
    monkeypatch.setattr(models_mod, "gpu_free_mb", lambda index=0: 3000)
    seen = models_seen(HAPPY)
    result = _orch(repo, _config(reviewer="")).run("add x", branch="none")
    assert seen[-1] == MAIN
    assert result.reviewer_model_note == ""


def test_verdicts_recorded_in_order(repo, fake_client_factory):
    fake_client_factory([PLAN, TESTER_WRITE, TESTER_DONE, CODER_WRITE_MARKER,
                         CODER_DONE, DENY, CODER_DONE, APPROVE])
    result = _orch(repo).run("add x", branch="verdicts")
    assert result.status == "merged"
    assert result.verdicts == ["DENY", "APPROVE"]
    assert result.revisions == 1
