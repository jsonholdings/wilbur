"""2.52.0 project-directory awareness: last project, `No module named` hint,
global ~/.config/wilbur/WILBUR.md. Every test points HOME and both env paths
at tmp_path, so no real file is read or written."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.__main__ as main_mod
import wilbur.agent as agent_mod
from wilbur import projectdir
from wilbur.config import Config
from wilbur.llm import ToolCall
from wilbur.repl import Repl
from wilbur.tools.base import Approval

from conftest import FakeClient, say


@pytest.fixture
def home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WILBUR_LAST_PROJECT", str(tmp_path / "state" / "last-project"))
    monkeypatch.setenv("WILBUR_GLOBAL_INSTRUCTIONS", str(tmp_path / "global" / "WILBUR.md"))
    return home


def _repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def _venv(base: Path, name: str = ".venv") -> Path:
    python = base / name / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    return python


# ---------------------------------------------------------------- last project

def test_record_and_read_last_project(home, tmp_path):
    repo = _repo(tmp_path / "proj")
    (repo / "sub").mkdir()
    assert projectdir.record_last_project(repo / "sub") == repo.resolve()
    assert projectdir.last_project() == repo.resolve()


def test_no_record_outside_a_repo_or_for_home(home, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert projectdir.record_last_project(plain) is None
    _repo(home)
    assert projectdir.record_last_project(home) is None
    assert projectdir.last_project() is None


def test_vanished_last_project_is_ignored(home, tmp_path):
    repo = _repo(tmp_path / "gone")
    projectdir.record_last_project(repo)
    (repo / ".git").rmdir()
    repo.rmdir()
    assert projectdir.last_project() is None


def test_startup_dir_switches_only_from_home(home, tmp_path):
    repo = _repo(tmp_path / "proj")
    projectdir.record_last_project(repo)
    assert projectdir.startup_dir(str(home), explicit_dir=False, here=False) == repo.resolve()
    # controls: --here, -C, and a non-$HOME cwd never switch
    assert projectdir.startup_dir(str(home), explicit_dir=False, here=True) is None
    assert projectdir.startup_dir(str(home), explicit_dir=True, here=False) is None
    assert projectdir.startup_dir(str(tmp_path), explicit_dir=False, here=False) is None


def test_enter_project_chdirs_and_prints(home, tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "proj")
    projectdir.record_last_project(repo)
    monkeypatch.chdir(home)
    args = argparse.Namespace(cwd=str(home), here=False)
    main_mod._enter_project(args, explicit_dir=False)
    assert args.cwd == str(repo.resolve())
    assert Path.cwd() == repo.resolve()
    out = capsys.readouterr().out
    assert str(repo.resolve()) in out and "--here" in out


def test_enter_project_with_here_stays(home, tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "proj")
    projectdir.record_last_project(repo)
    monkeypatch.chdir(home)
    args = argparse.Namespace(cwd=str(home), here=True)
    main_mod._enter_project(args, explicit_dir=False)
    assert args.cwd == str(home) and Path.cwd() == home
    assert "switched" not in capsys.readouterr().out


def test_enter_project_in_repo_records_it(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "proj")
    args = argparse.Namespace(cwd=str(repo), here=False)
    main_mod._enter_project(args, explicit_dir=False)
    assert projectdir.last_project() == repo.resolve()


def test_oneshot_never_switches(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "proj")
    projectdir.record_last_project(repo)
    monkeypatch.chdir(home)
    seen = {}
    monkeypatch.setattr(main_mod, "check_backend", lambda config: "ollama")
    monkeypatch.setattr(main_mod, "_oneshot",
                        lambda config, cwd, prompt, auto: seen.setdefault("cwd", cwd) and 0)
    main_mod.main(["-p", "hi"])
    assert seen["cwd"] == str(home)
    assert Path.cwd() == home


def test_cd_prefix_dir():
    assert projectdir.cd_prefix_dir("cd /a/b && pytest") == Path("/a/b")
    assert projectdir.cd_prefix_dir("cd '/a b'; ls") == Path("/a b")
    assert projectdir.cd_prefix_dir("cd rel && ls") is None
    assert projectdir.cd_prefix_dir("ls /a") is None


def test_run_bash_cd_prefix_records_last_project(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "proj")
    plain = tmp_path / "plain"
    plain.mkdir()
    agent = agent_mod.Agent(Config(), str(home), approve=lambda t, d: Approval.GRANTED)
    agent._execute(ToolCall("run_bash", {"command": f"cd {plain} && true"}))
    assert projectdir.last_project() is None, "a non-repo dir must not be recorded"
    agent._execute(ToolCall("run_bash", {"command": f"cd {repo} && true"}))
    assert projectdir.last_project() == repo.resolve()


# ---------------------------------------------------------------- module hint

def test_hint_names_interpreter_cwd_and_venv(home, tmp_path):
    repo = _repo(tmp_path / "proj")
    venv_py = _venv(repo)
    out = f"{home}/venv/bin/python: No module named pytest\n"
    hint = projectdir.module_hint(f"cd {repo} && {home}/venv/bin/python -m pytest", out, str(home))
    lines = hint.splitlines()
    assert 1 < len(lines) <= 3 and all(l.startswith("[harness]") for l in lines)
    assert f"{home}/venv/bin/python" in hint and str(repo) in hint
    assert f"{venv_py} -m pip install pytest" in hint


def test_hint_from_import_error_uses_command_interpreter(home, tmp_path):
    repo = _repo(tmp_path / "proj")
    sub = repo / "pkg"
    sub.mkdir()
    venv_py = _venv(repo, "venv")
    out = "Traceback...\nModuleNotFoundError: No module named 'fastapi'\n"
    hint = projectdir.module_hint("./venv/bin/python app.py", out, str(sub))
    assert "'fastapi'" in hint and str(sub / "venv" / "bin" / "python") in hint
    assert str(venv_py) in hint, "the git root's venv is offered"


def test_hint_without_venv_says_so(home, tmp_path):
    hint = projectdir.module_hint("python3 x.py", "No module named 'yaml'", str(tmp_path))
    assert "no project venv" in hint and len(hint.splitlines()) == 2


def test_no_hint_without_the_error(home, tmp_path):
    _venv(tmp_path)
    assert projectdir.module_hint("python3 -m pytest", "3 passed", str(tmp_path)) == ""
    assert projectdir.module_hint("ls", "", str(tmp_path)) == ""


def test_agent_appends_hint_to_failing_run_bash(home, tmp_path):
    repo = _repo(tmp_path / "proj")
    _venv(repo)
    agent = agent_mod.Agent(Config(), str(repo), approve=lambda t, d: Approval.GRANTED)
    bad = agent._execute(ToolCall("run_bash", {"command": "python3 -c 'import nosuchmod_xyz'"}))
    assert "No module named 'nosuchmod_xyz'" in bad and "[harness] No module named" in bad
    good = agent._execute(ToolCall("run_bash", {"command": "echo ok"}))
    assert "[harness] No module" not in good


# ---------------------------------------------------------------- global instructions

def _write_global(text: str) -> Path:
    path = projectdir.global_instructions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_global_instructions_absent_then_present(home):
    assert projectdir.global_instructions() == ""
    _write_global("GLOBAL-RULE-42")
    assert "GLOBAL-RULE-42" in projectdir.global_instructions()


def test_global_instructions_capped(home):
    _write_global("x" * 9000)
    assert projectdir.global_instructions().count("x") <= projectdir.GLOBAL_INSTRUCTIONS_MAX + 1


def _repl(monkeypatch, cwd: Path) -> Repl:
    monkeypatch.setattr(agent_mod, "OllamaClient", FakeClient([say("ok")]))
    return Repl(Config(), str(cwd))


def test_system_prompt_order_persona_global_project(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "proj")
    (repo / "WILBUR.md").write_text("PROJECT-RULE-7")
    _write_global("GLOBAL-RULE-42")
    system = _repl(monkeypatch, repo).agent.messages[0]["content"]
    assert "GLOBAL-RULE-42" in system and "PROJECT-RULE-7" in system
    assert system.index("GLOBAL-RULE-42") < system.index("PROJECT-RULE-7")


def test_system_prompt_without_global_file(home, tmp_path, monkeypatch):
    system = _repl(monkeypatch, tmp_path).agent.messages[0]["content"]
    assert "Global instructions" not in system


def test_cd_command_moves_session_and_reloads_instructions(home, tmp_path, monkeypatch, capsys):
    first = tmp_path / "first"
    first.mkdir()
    repo = _repo(tmp_path / "proj")
    (repo / "WILBUR.md").write_text("PROJECT-RULE-7")
    repl = _repl(monkeypatch, first)
    assert "PROJECT-RULE-7" not in repl.agent.messages[0]["content"]
    repl._command(f"/cd {repo}")
    assert repl.cwd == str(repo.resolve()) and repl.agent.ctx.cwd == str(repo.resolve())
    assert "PROJECT-RULE-7" in repl.agent.messages[0]["content"]
    assert projectdir.last_project() == repo.resolve()
    repl._command(f"/cd {tmp_path / 'nope'}")
    assert repl.cwd == str(repo.resolve()), "a missing dir leaves cwd alone"
    assert "not a directory" in capsys.readouterr().out
