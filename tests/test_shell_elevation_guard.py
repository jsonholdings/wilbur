"""run_bash refuses sudo/su/doas/pkexec without executing them."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools import build_registry
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.shell import elevation_program


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


BLOCKED = [
    "sudo /usr/bin/true",
    "  sudo -n true",
    "/usr/bin/sudo ls",
    "true && sudo ls",
    "true; sudo ls",
    "echo hi | sudo tee /etc/x",
    "false || su -c id",
    "env sudo ls",
    "env FOO=1 sudo ls",
    "FOO=1 sudo ls",
    "command sudo ls",
    "nohup sudo ls &",
    "timeout 5 doas ls",
    "pkexec /bin/sh -c id",
    "(sudo ls)",
    "echo $(sudo id)",
    "echo `sudo id`",
    "'sudo' ls",
    "true\nsudo ls",
    "xargs sudo rm < list",
]

ALLOWED = [
    "echo pseudo code",
    "cat sudoers.txt",
    "grep -n sudo README.md",
    "echo 'run sudo later'",
    "ls /usr/bin/sudoku",
    "python3 -c 'print(1)'",
    "git log --grep=su",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_elevation_detected(command):
    assert elevation_program(command) is not None


@pytest.mark.parametrize("command", ALLOWED)
def test_no_false_positive(command):
    assert elevation_program(command) is None


def test_refused_without_running(tmp_path, ctx):
    marker = tmp_path / "ran"
    out = build_registry()["run_bash"].run(
        {"command": f"touch {marker} && sudo true"}, ctx)
    assert out.is_error
    assert "not available" in out.output and "Do not retry" in out.output
    assert not marker.exists()


def test_approval_not_requested_for_elevation(tmp_path):
    asked = []
    ctx = ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                      approve=lambda tool, detail: asked.append(tool) or Approval.GRANTED)
    build_registry()["run_bash"].run({"command": "sudo ls"}, ctx)
    assert asked == []


def test_plain_command_still_runs(ctx):
    out = build_registry()["run_bash"].run({"command": "echo pseudo"}, ctx)
    assert not out.is_error and "pseudo" in out.output
