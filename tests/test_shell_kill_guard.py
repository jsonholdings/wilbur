"""run_bash refuses kill/pkill/killall aimed at PID 1, -1, 0 or Wilbur itself
(parity G5, bench task 32_kill_pid_list), in every approval mode."""
import os
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools import build_registry
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.killguard import kill_refusal


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


def _own_comm() -> str:
    return Path(f"/proc/{os.getpid()}/comm").read_text().strip()


BLOCKED = [
    "kill 1",
    "kill -9 1",
    "kill -TERM 1",
    "kill -s KILL 1",
    "kill -- -1",
    "kill -9 -1",
    "kill 0",
    "kill $PPID",
    "kill -15 ${PPID}",
    f"kill {os.getpid()}",
    f"kill -TERM {os.getppid()}",
    f"kill -- -{os.getpgid(0)}",
    f"sleep 1; kill {os.getpid()}",
    f"true && timeout 5 kill -9 {os.getpid()}",
    "pkill -u $USER",
    "killall -u someone",
    "pkill init",
    "killall systemd",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_kill_refused(command):
    assert kill_refusal(command) is not None


def test_pkill_matching_own_process_refused():
    assert kill_refusal(f"pkill {_own_comm()}") is not None
    assert kill_refusal(f"killall {_own_comm()}") is not None


ALLOWED = [
    "kill 123456",
    "kill -9 123456 123457",
    "kill $!",
    "kill %1",
    "kill -l",
    "pkill -f my_test_server_zz",
    "killall my_test_server_zz",
    "echo kill 1",
    "grep -r 'kill 1' .",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_kill_allowed(command):
    assert kill_refusal(command) is None


def test_refused_without_running_and_without_asking(tmp_path):
    marker = tmp_path / "ran"
    asked = []
    ctx = ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                      approve=lambda tool, detail: asked.append(tool) or Approval.GRANTED)
    out = build_registry()["run_bash"].run(
        {"command": f"touch {marker} && kill -TERM {os.getpid()}"}, ctx)
    assert out.is_error and "Refused without running" in out.output
    assert "Wilbur's own process" in out.output
    assert not marker.exists() and asked == []


def test_control_kill_of_own_child_still_runs(ctx):
    """Control: the guard must not stop a normal kill of a child process."""
    child = subprocess.Popen(["sleep", "30"])
    try:
        out = build_registry()["run_bash"].run({"command": f"kill {child.pid}"}, ctx)
        assert not out.is_error, out.output
        assert child.wait(timeout=10) == -15
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_pkill_newest_flag_takes_no_value():
    """`pkill -n` means newest, not an option with a value (verifier, 2.54.0):
    the pattern after it is still checked."""
    assert kill_refusal(f"pkill -n {_own_comm()}") is not None
