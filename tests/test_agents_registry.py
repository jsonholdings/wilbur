"""wilbur.tools.agents_registry and the /agents REPL command."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import wilbur.agent as agent_mod
from wilbur.config import Config
from wilbur.repl import Repl
from wilbur.tools import agents_registry

from conftest import FakeClient, say


@pytest.fixture(autouse=True)
def clear_registry():
    agents_registry.clear()
    yield
    agents_registry.clear()


def test_register_update_finish_snapshot_roundtrip():
    rec = agents_registry.register("search the repo", "explore")
    assert rec.status == "running"
    assert agents_registry.running() == [rec]

    agents_registry.update(rec.id, current="tool: grep", round=1)
    got = agents_registry.snapshot()[0]
    assert got.current == "tool: grep"
    assert got.round == 1

    agents_registry.finish(rec.id, "done")
    assert agents_registry.running() == []
    assert agents_registry.snapshot()[0].status == "done"


def test_finished_records_are_capped():
    for i in range(agents_registry._MAX_FINISHED + 5):
        rec = agents_registry.register(f"job {i}", "explore")
        agents_registry.finish(rec.id, "done")
    assert len(agents_registry.snapshot()) == agents_registry._MAX_FINISHED
    # the oldest ones were dropped, newest kept
    labels = [r.label for r in agents_registry.snapshot()]
    assert "job 0" not in labels
    assert f"job {agents_registry._MAX_FINISHED + 4}" in labels


def _make_repl(monkeypatch, tmp_path):
    client = FakeClient([say("noop")])
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    repl = Repl(Config(), str(tmp_path))
    repl.manager.available = lambda: True
    return repl


def test_agents_command_with_no_subagents(monkeypatch, tmp_path, capsys):
    repl = _make_repl(monkeypatch, tmp_path)
    repl._agents_command()
    out = capsys.readouterr().out
    assert "no subagents" in out


def test_agents_command_lists_running_and_finished(monkeypatch, tmp_path, capsys):
    repl = _make_repl(monkeypatch, tmp_path)
    running = agents_registry.register("hunt for bug", "general")
    done = agents_registry.register("survey the repo", "explore")
    agents_registry.finish(done.id, "done")

    repl._agents_command()
    out = capsys.readouterr().out
    assert "hunt for bug" in out and "running" in out
    assert "survey the repo" in out and "done" in out


def test_agents_status_line_reflects_running_count(monkeypatch, tmp_path):
    repl = _make_repl(monkeypatch, tmp_path)
    assert repl._agents_status_line() == ""

    rec = agents_registry.register("job", "explore")
    assert "1 subagent running" in repl._agents_status_line()

    rec2 = agents_registry.register("job2", "explore")
    assert "2 subagents running" in repl._agents_status_line()

    agents_registry.finish(rec.id, "done")
    agents_registry.finish(rec2.id, "done")
    assert repl._agents_status_line() == ""


def test_help_mentions_agents_command(monkeypatch, tmp_path, capsys):
    repl = _make_repl(monkeypatch, tmp_path)
    repl._command("/help")
    out = capsys.readouterr().out
    assert "/agents" in out
