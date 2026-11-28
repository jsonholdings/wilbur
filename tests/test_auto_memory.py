"""2.74.0 auto project memory (wilbur.memory). Every test uses a temp HOME and
a temp WILBUR_MEMORY_DIR, so the owner's real memory is never read or written."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.agent as agent_mod
from wilbur import memory
from wilbur.config import Config
from wilbur.fake_backend import FakeClient
from wilbur.llm import Reply, ToolCall
from wilbur.repl import Repl


@pytest.fixture
def home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WILBUR_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("WILBUR_LAST_PROJECT", str(tmp_path / "state" / "last-project"))
    monkeypatch.setenv("WILBUR_GLOBAL_INSTRUCTIONS", str(tmp_path / "global" / "WILBUR.md"))
    monkeypatch.setenv("WILBUR_SECRET_FILES", "")
    return home


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    return path


def _commit(repo: Path, msg: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", msg],
                   check=True, env=env)


def _repl(monkeypatch, cwd: Path, replies=None, config: Config | None = None) -> Repl:
    monkeypatch.setattr(agent_mod, "OllamaClient", FakeClient(replies or [Reply(content="ok", tool_calls=[])]))
    return Repl(config or Config(), str(cwd))


def _session_one(monkeypatch, repo: Path) -> None:
    """Session 1: the user asks for a task, the model writes a file and plans
    a second step it does not reach, then the session exits."""
    repl = _repl(monkeypatch, repo, [
        Reply(content="", tool_calls=[ToolCall(name="todo_write", arguments={"todos": [
            {"task": "add the parser", "status": "completed"},
            {"task": "wire the parser into cli.py", "status": "pending"}]})]),
        Reply(content="", tool_calls=[ToolCall(name="write_file", arguments={
            "path": "parser.py", "content": "def parse():\n    return 1\n"})]),
        Reply(content="parser added; cli wiring next", tool_calls=[]),
    ])
    repl.agent.ctx.approve = lambda *_: __import__("wilbur.tools.base", fromlist=["Approval"]).Approval.GRANTED
    repl.agent.run("build the config parser")
    repl._save_memory_summary()


# ---------------------------------------------------------------- loading

def test_fresh_project_loads_nothing(home, tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "proj")
    repl = _repl(monkeypatch, repo)
    system = repl.agent.messages[0]["content"]
    assert memory.RECALL_LABEL not in system
    assert memory.load_block(repo) == "" and memory.start_brief(repo) == ""


def test_second_session_loads_first_sessions_summary(home, tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path / "proj")
    _commit(repo, "initial")
    _session_one(monkeypatch, repo)
    pdir = memory.project_dir(repo)
    files = list((pdir / "sessions").glob("*.md"))
    assert len(files) == 1
    _commit(repo, "wire parser by hand")
    capsys.readouterr()
    second = _repl(monkeypatch, repo)
    system = second.agent.messages[0]["content"]
    assert memory.RECALL_LABEL in system
    assert "build the config parser" in system
    assert "wire the parser into cli.py" in system and "parser.py" in system
    out = capsys.readouterr().out
    assert "Resuming proj" in out and "wire the parser into cli.py" in out
    assert "wire parser by hand" in out and "wire parser by hand" in system
    assert "initial" not in out, "only commits made since the last session"


def test_control_auto_memory_off_loads_and_writes_nothing(home, tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "proj")
    _session_one(monkeypatch, repo)
    config = Config()
    config.auto_memory = False
    off = _repl(monkeypatch, repo, config=config)
    assert memory.RECALL_LABEL not in off.agent.messages[0]["content"]
    assert "memory_save" not in off.agent.registry
    assert "memory_save" in _repl(monkeypatch, repo).agent.registry


def test_home_is_never_a_project(home):
    assert memory.project_dir(home) is None
    assert memory.save_fact(home, "x", "y")[0] == "error"


# ---------------------------------------------------------------- store

def test_secrets_are_redacted_on_every_write(home, tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "proj")
    secret_file = tmp_path / "secrets.env"
    # Secret shapes are assembled at runtime so the mirror's scrub check
    # does not read the test source itself as a leaked credential.
    token_key, pw_key = "DEPLOY_" + "TOKEN", "pass" + "word"
    gh_token = "ghp" + "_" + "0123456789abcdefABCDEF"
    secret_file.write_text(f"{token_key}=hunter2-very-secret\n")
    monkeypatch.setenv("WILBUR_SECRET_FILES", str(secret_file))
    memory.save_fact(repo, "deploy", f"deploy with api{'_'}key=abc123def and hunter2-very-secret "
                     f"and {gh_token}")
    text = memory.read_fact(repo, "deploy")
    assert "abc123def" not in text and "hunter2-very-secret" not in text
    assert gh_token not in text and "<REDACTED>" in text
    memory.write_session_summary(repo, "s1", [{"role": "user", "content": f"use {pw_key}=letmein9"}])
    pdir = memory.project_dir(repo)
    for path in pdir.rglob("*.md"):
        assert "letmein9" not in path.read_text() and "abc123def" not in path.read_text()
    assert oct(pdir.stat().st_mode & 0o777) == "0o700"
    # control: the redactor is what removed it -- the plain text survives otherwise
    memory.save_fact(repo, "plain", "deploy with make ship")
    assert "make ship" in memory.read_fact(repo, "plain")


def test_duplicates_update_in_place(home, tmp_path):
    repo = _git_repo(tmp_path / "proj")
    assert memory.save_fact(repo, "Test Command", "run make test") == ("created", "test-command")
    assert memory.save_fact(repo, "test-command", "run make check") == ("updated", "test-command")
    assert memory.save_fact(repo, "other", "run make check") == ("duplicate", "test-command")
    facts = memory.list_facts(repo)
    assert [f["name"] for f in facts] == ["test-command"]
    assert "make check" in facts[0]["body"]
    index = (memory.project_dir(repo) / "MEMORY.md").read_text()
    assert index.count("test-command") == 1
    assert memory.forget(repo, "test-command") and memory.list_facts(repo) == []
    assert not memory.forget(repo, "test-command")


def test_size_cap_holds(home, tmp_path):
    repo = _git_repo(tmp_path / "proj")
    for n in range(80):
        memory.save_fact(repo, f"fact-{n}", f"body {n} " + "x" * 50, description="d" * 140)
    memory.write_session_summary(repo, "big", [{"role": "user", "content": "goal " + "y" * 9000}])
    block = memory.load_block(repo)
    assert len(block) <= memory.LOAD_MAX_CHARS
    assert "memory_read" in block
    # control: under the cap nothing is cut
    small = _git_repo(tmp_path / "small")
    memory.save_fact(small, "one", "only fact")
    assert "truncated" not in memory.load_block(small)


def test_old_session_summaries_are_pruned(home, tmp_path):
    repo = _git_repo(tmp_path / "proj")
    for n in range(memory.SESSIONS_KEPT + 3):
        memory.write_session_summary(repo, f"s{n}", [{"role": "user", "content": f"task {n}"}])
    files = list((memory.project_dir(repo) / "sessions").glob("*.md"))
    assert len(files) == memory.SESSIONS_KEPT
    memory.write_session_summary(repo, "s3", [{"role": "user", "content": "again"}])
    assert len(list((memory.project_dir(repo) / "sessions").glob("*-s3.md"))) <= 1


# ---------------------------------------------------------------- prompt prefix

def test_prompt_prefix_stays_stable_across_turns_and_saves(home, tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "proj")
    memory.save_fact(repo, "build", "build with make")
    client = FakeClient([
        Reply(content="", tool_calls=[ToolCall(name="memory_save", arguments={
            "name": "new-fact", "body": "learned mid-session"})]),
        Reply(content="saved", tool_calls=[]),
        Reply(content="second turn", tool_calls=[]),
    ])
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    repl = Repl(Config(), str(repo))
    system_before = repl.agent.messages[0]["content"]
    assert "build with make" in system_before.replace("build (project): build with make", "build with make")
    repl.agent.run("remember something")
    repl.agent.run("next")
    sent = client.seen
    assert len(sent) == 3
    assert memory.read_fact(repo, "new-fact") is not None, "control: the save really happened"
    for prev, nxt in zip(sent, sent[1:]):
        assert [json.dumps(m, sort_keys=True) for m in nxt[:len(prev)]] == \
               [json.dumps(m, sort_keys=True) for m in prev]
    assert all(s[0]["content"] == system_before for s in sent)
    assert "learned mid-session" not in system_before


# ---------------------------------------------------------------- tools + /memory

def test_memory_tools_round_trip(home, tmp_path):
    from wilbur.tools.base import ToolContext
    from wilbur.tools.memory_tools import MemoryForget, MemoryRead, MemorySave
    repo = _git_repo(tmp_path / "proj")
    ctx = ToolContext(cwd=str(repo), config=Config(), read_files={}, approve=lambda *_: None)
    assert "created" in MemorySave().run({"name": "a", "body": "x1"}, ctx).output
    assert "not saved" in MemorySave().run({"name": "b", "body": "x1"}, ctx).output
    assert "recalled" in MemoryRead().run({"name": "a"}, ctx).output
    assert "- a (project)" in MemoryRead().run({}, ctx).output
    assert MemoryForget().run({"name": "nope"}, ctx).is_error
    assert "forgot a" in MemoryForget().run({"name": "a"}, ctx).output


def test_slash_memory_lists_shows_forgets(home, tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path / "proj")
    memory.save_fact(repo, "lint", "run ruff")
    repl = _repl(monkeypatch, repo)
    repl._command("/memory")
    assert "lint (project)" in capsys.readouterr().out
    repl._command("/memory show lint")
    assert "run ruff" in capsys.readouterr().out
    repl._command("/memory forget lint")
    assert memory.list_facts(repo) == []


def test_clear_writes_a_summary(home, tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "proj")
    repl = _repl(monkeypatch, repo)
    repl.agent.messages.append({"role": "user", "content": "refactor the loader"})
    repl._command("/clear")
    newest = memory.newest_session(repo)
    assert newest is not None and "refactor the loader" in newest[1]
    assert "refactor the loader" in repl.agent.messages[0]["content"], "the new agent loads it"
