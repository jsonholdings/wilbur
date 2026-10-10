"""run_bash edge behaviour: exit codes, stderr capture, working directory.

Exercises the Bash tool (wilbur/tools/shell.py) against a ToolContext whose
cwd is a tmp_path directory, using only harmless commands (true, false,
echo, pwd). No network, no GPU, no real ~/.config/wilbur or ~/.local
(the conftest `isolate_wilbur_home` fixture already redirects those).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools import build_registry
from wilbur.tools.base import Approval, ToolContext


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


def test_zero_exit_reports_success(ctx):
    out = build_registry()["run_bash"].run({"command": "true"}, ctx)
    assert not out.is_error
    assert out.output == "(no output)"


def test_nonzero_exit_reported_with_code(ctx):
    out = build_registry()["run_bash"].run({"command": "false"}, ctx)
    assert out.is_error
    assert "exit 1" in out.output


def test_nonzero_exit_code_is_the_actual_code(tmp_path):
    # `echo` with a non-zero status via the shell's `exit` builtin is not
    # allowed (only true/false/echo/pwd), so use a command that exits 3:
    # `echo hi; exit 3` -- still only harmless builtins.
    ctx = ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                      approve=lambda tool, detail: Approval.GRANTED)
    out = build_registry()["run_bash"].run({"command": "echo hi; exit 3"}, ctx)
    assert out.is_error
    assert "exit 3" in out.output
    assert "hi" in out.output


def test_stderr_is_captured(ctx):
    out = build_registry()["run_bash"].run(
        {"command": "echo out-msg 1>&2"}, ctx)
    assert not out.is_error
    assert "out-msg" in out.output


def test_working_directory_is_context_cwd(ctx):
    out = build_registry()["run_bash"].run({"command": "pwd"}, ctx)
    assert not out.is_error
    assert out.output.strip() == str(Path(ctx.cwd).resolve())


def test_cwd_is_not_the_test_process_cwd(tmp_path, monkeypatch):
    # Run from a different process cwd and prove the tool still uses ctx.cwd.
    monkeypatch.chdir(Path.cwd())
    other = tmp_path / "workdir"
    other.mkdir()
    ctx = ToolContext(cwd=str(other), config=Config(), read_files={},
                      approve=lambda tool, detail: Approval.GRANTED)
    out = build_registry()["run_bash"].run({"command": "pwd"}, ctx)
    assert out.output.strip() == str(other.resolve())
