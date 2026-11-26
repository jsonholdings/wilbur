"""2.58.0: environment facts in the static prompt, and one-line recovery hints
on failed tool calls (the 2.57.2 trace: 15 `python: not found`, ~6 wrong
cwd/path). Every hint has a control: no hint on success or an unrelated error."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.agent import Agent
from wilbur.config import Config
from wilbur.tools import build_registry, hints
from wilbur.tools.base import Approval, ToolContext

ONLY_PY3 = {"python3": "/usr/bin/python3"}.get
BOTH = {"python3": "/usr/bin/python3", "python": "/usr/bin/python"}.get


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


@pytest.fixture
def tools():
    return build_registry()


# --- environment facts ------------------------------------------------------

def test_facts_say_use_python3_when_python_is_absent(tmp_path):
    facts = hints.env_facts(str(tmp_path), which=ONLY_PY3)
    assert f"Project root: {tmp_path}" in facts
    assert "no `cd` needed" in facts
    assert "`python` is NOT on PATH -- use `python3`" in facts


def test_facts_do_not_say_use_python3_when_python_is_present(tmp_path):
    facts = hints.env_facts(str(tmp_path), which=BOTH)
    assert "NOT on PATH" not in facts and "use `python3`" not in facts
    assert "`python` and `python3` are on PATH" in facts


def test_facts_name_the_project_venv(tmp_path):
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("")
    assert f"Project venv: {venv / 'python'}" in hints.env_facts(str(tmp_path), which=ONLY_PY3)


def test_facts_sit_in_the_static_system_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(hints.shutil, "which", ONLY_PY3)
    agent = Agent(Config(), str(tmp_path), approve=lambda *_: Approval.GRANTED,
                  system_extra="EXTRA")
    system = agent.messages[0]["content"]
    assert "use `python3`" in system
    # before the per-session extras, i.e. part of the fixed prefix
    assert system.index("Environment (probed") < system.index("EXTRA")


# --- run_bash: command not found ----------------------------------------------

def test_python_not_found_hints_python3():
    out = "exit 127\n/bin/sh: 1: python: not found"
    hint = hints.missing_command_hint(out, "/nowhere", which=ONLY_PY3)
    assert "`python` is not on PATH here; use `python3`." in hint
    assert "Retrying the same command will fail again." in hint


def test_pip_not_found_hints_python3_m_pip():
    out = "exit 127\nbash: pip: command not found"
    assert "use `python3 -m pip`" in hints.missing_command_hint(out, "/nowhere", which=ONLY_PY3)


def test_not_found_control_success_and_unrelated_error():
    assert hints.missing_command_hint("hello", "/nowhere", which=ONLY_PY3) == ""
    assert hints.missing_command_hint("exit 1\nTraceback ...", "/nowhere", which=ONLY_PY3) == ""


def test_run_bash_end_to_end_not_found_hint(ctx, tools):
    r = tools["run_bash"].run({"command": "wilbur_no_such_prog_xyz --version"}, ctx)
    assert r.is_error and "exit 127" in r.output
    assert "`wilbur_no_such_prog_xyz` is not on PATH here." in r.output
    ok = tools["run_bash"].run({"command": "echo fine"}, ctx)
    assert "[harness]" not in ok.output


# --- run_bash: stray cd -------------------------------------------------------

def test_cd_into_missing_dir_hints_drop_the_cd(tmp_path):
    out = "exit 2\n/bin/sh: 1: cd: can't cd to /x"
    hint = hints.cd_hint(f"cd {tmp_path}/nope && python3 -c 1", out, str(tmp_path))
    assert "no such directory" in hint
    assert f"run_bash already runs in {tmp_path}; drop the cd." in hint


def test_cd_into_ancestor_hints_drop_the_cd(ctx, tools, tmp_path):
    work = tmp_path / ".bench-work-x-1"
    work.mkdir()
    ctx.cwd = str(work)
    r = tools["run_bash"].run({"command": f"cd {tmp_path} && test -f marker_absent"}, ctx)
    assert r.is_error and "went ABOVE the project root" in r.output
    assert f"run_bash already runs in {work}; drop the cd." in r.output


def test_cd_hint_controls(ctx, tools, tmp_path):
    # success with an ancestor cd: no hint
    sub = tmp_path / "sub"
    sub.mkdir()
    ctx.cwd = str(sub)
    ok = tools["run_bash"].run({"command": f"cd {tmp_path} && true"}, ctx)
    assert "[harness]" not in ok.output
    # failure with cd to the root itself, or to a sibling that exists: no hint
    assert hints.cd_hint(f"cd {sub} && false", "exit 1", str(sub)) == ""
    other = tmp_path / "other"
    other.mkdir()
    assert hints.cd_hint(f"cd {other} && false", "exit 1", str(sub)) == ""
    # failure with no cd: no hint
    assert hints.cd_hint("false", "exit 1", str(sub)) == ""


# --- file tools: absolute path outside the project ----------------------------

def test_write_at_filesystem_root_hints_project_path(ctx, tools, tmp_path):
    r = tools["write_file"].run({"path": "/wilbur_test_ranges_xyz.py", "content": "x"}, ctx)
    if not r.is_error:  # running as root would succeed; then there is nothing to test
        Path("/wilbur_test_ranges_xyz.py").unlink()
        pytest.skip("filesystem root is writable here")
    assert "Could not write" in r.output
    assert f"did you mean {tmp_path / 'wilbur_test_ranges_xyz.py'}" in r.output


def test_read_outside_path_hints_existing_project_file(ctx, tools, tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n")
    r = tools["read_file"].run({"path": "/nonexistent-dir-xyz/mod.py"}, ctx)
    assert r.is_error and f"did you mean {tmp_path / 'mod.py'}" in r.output


def test_file_hint_controls(ctx, tools, tmp_path):
    # outside path with no project counterpart: no hint
    r = tools["read_file"].run({"path": "/nonexistent-dir-xyz/other.py"}, ctx)
    assert r.is_error and "[harness]" not in r.output
    # missing file INSIDE the project: no hint
    r = tools["read_file"].run({"path": str(tmp_path / "missing.py")}, ctx)
    assert r.is_error and "[harness]" not in r.output
    # success: no hint
    ok = tools["write_file"].run({"path": "new.py", "content": "x"}, ctx)
    assert not ok.is_error and "[harness]" not in ok.output


# --- grep / list_files / glob: missing path ------------------------------------

def test_grep_missing_dir_hints_root_and_nearest(ctx, tools, tmp_path):
    (tmp_path / "src" / "etl").mkdir(parents=True)
    (tmp_path / "src" / "etl" / "a.py").write_text("needle\n")
    r = tools["grep"].run({"pattern": "needle", "path": "etl"}, ctx)
    assert r.is_error and f"project root {tmp_path}" in r.output
    assert "nearest match: 'src/etl'" in r.output


def test_list_files_missing_dir_hints_root(ctx, tools, tmp_path):
    r = tools["list_files"].run({"path": "nope"}, ctx)
    assert r.is_error and f"project root {tmp_path}" in r.output
    assert "list_files('.')" in r.output


def test_glob_missing_dir_hints_root(ctx, tools, tmp_path):
    r = tools["glob"].run({"pattern": "*.py", "path": "nope"}, ctx)
    assert r.is_error and "[harness] nope does not exist" in r.output


def test_search_hint_controls(ctx, tools, tmp_path):
    (tmp_path / "a.py").write_text("needle\n")
    ok = tools["grep"].run({"pattern": "needle", "path": "a.py"}, ctx)
    assert not ok.is_error and "[harness]" not in ok.output
    none = tools["grep"].run({"pattern": "absent"}, ctx)
    assert not none.is_error and "[harness]" not in none.output
    # list_files on a FILE (exists, not a dir): plain error, no hint
    r = tools["list_files"].run({"path": "a.py"}, ctx)
    assert r.is_error and "[harness]" not in r.output
