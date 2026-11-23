"""dual-3090-enable: multi-card VRAM checks and the optional second Ollama
server for `task` subagents and the compaction summariser. Fakes only: a
two-card nvidia-smi output and a recording OllamaClient; no GPU, no model."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import wilbur.agent as agent_mod
from wilbur.config import Config
from wilbur.models import config_gpu_indices, gpu_free_mb_for
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.task import Task

TWO_CARDS = "20000, 24576\n9000, 24576\n"


def fake_smi(monkeypatch, stdout):
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout, ""))


def test_default_is_single_card_gpu_index(monkeypatch):
    fake_smi(monkeypatch, TWO_CARDS)
    assert config_gpu_indices(Config()) == [0]
    assert gpu_free_mb_for(Config()) == 20000
    assert gpu_free_mb_for(Config(gpu_index=1)) == 9000


def test_gpu_indices_sum_only_the_listed_cards(monkeypatch):
    fake_smi(monkeypatch, TWO_CARDS)
    assert gpu_free_mb_for(Config(gpu_indices=[0, 1])) == 29000
    assert gpu_free_mb_for(Config(gpu_indices=[1, 1])) == 9000


def test_missing_listed_card_is_unknown_not_a_fit(monkeypatch):
    """Control: on a one-card host a two-card config must not read as fit."""
    fake_smi(monkeypatch, "20000, 24576\n")
    assert gpu_free_mb_for(Config(gpu_indices=[0, 1])) is None


class _Sub:
    last = None

    def __init__(self, **kw):
        self.kw = kw
        self.ctx = type("C", (), {"read_files": {}})()
        _Sub.last = self

    def run(self, prompt):
        return "ok"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "Agent", _Sub)
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda t, d: Approval.GRANTED)


def test_task_uses_main_server_by_default(ctx):
    Task().run({"prompt": "find x", "agent_type": "explore"}, ctx)
    assert _Sub.last.kw["config"].base_url == ctx.config.base_url


def test_task_routes_to_second_server_when_configured(ctx):
    ctx.config.subagent_base_url = "http://localhost:11435"
    ctx.config.subagent_gpu_index = 1
    ctx.config.gpu_indices = [0, 1]
    Task().run({"prompt": "find x", "agent_type": "explore"}, ctx)
    sub = _Sub.last.kw["config"]
    assert sub.base_url == "http://localhost:11435"
    assert sub.gpu_index == 1 and sub.gpu_indices == []
    assert ctx.config.base_url == "http://localhost:11434", "parent untouched"


class _Client:
    def __init__(self, base_url, model, timeout):
        self.base_url, self.model = base_url, model


def _agent(monkeypatch, tmp_path, **cfg):
    monkeypatch.setattr(agent_mod, "OllamaClient", _Client)
    return agent_mod.Agent(config=Config(**cfg), cwd=str(tmp_path), approve=lambda t, d: True)


def test_summariser_is_own_client_by_default(monkeypatch, tmp_path):
    a = _agent(monkeypatch, tmp_path)
    assert a._summariser_client() is a.client


def test_summariser_uses_second_server_when_configured(monkeypatch, tmp_path):
    a = _agent(monkeypatch, tmp_path, subagent_base_url="http://localhost:11435",
               subagent_model="small:7b")
    s = a._summariser_client()
    assert s is not a.client and s.base_url == "http://localhost:11435"
    assert s.model == "small:7b" and a._summariser_client() is s
    assert a.client.base_url == "http://localhost:11434"
