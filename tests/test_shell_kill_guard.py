"""run_bash/job_start refuse kill-family commands that can hit anything the
session did not start (parity G5, bench 32_kill_pid_list; incident 2026-11-27
21:29:31, when `pkill -f build` and `killall python3` ran for real).

HARD RULE for this file: no test executes a kill-family command. The guard is
tested as a pure parse-and-decide function; every run_bash/job_start call goes
through the autouse `no_execution` fake, which records the command instead of
running it. No subprocess is imported here.
"""
import importlib.util
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools import build_registry, contained, killguard
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.killguard import kill_refusal

FOREIGN_PID = 424242   # any running process the session did not start
SESSION_PID = 434343   # a process the session started
GONE_PID = 123456      # not running
_REAL_PROCESS_ORIGIN = killguard.process_origin


@pytest.fixture(autouse=True)
def no_execution(monkeypatch):
    """Nothing in this file reaches a real process: execution is a fake that
    records the command, and process ownership comes from a fixed table."""
    executed: list[str] = []

    def fake_shell(command, cwd, timeout, memory_max=None):
        executed.append(command)
        return contained.Completed(0, "", "")

    def refuse(*args, **kwargs):
        raise AssertionError(f"execution path reached: {args!r}")

    monkeypatch.setattr(contained, "shell", fake_shell)
    monkeypatch.setattr(contained, "run", refuse)
    monkeypatch.setattr(contained, "popen", refuse)
    origins = {FOREIGN_PID: "foreign", SESSION_PID: "session"}
    monkeypatch.setattr(killguard, "process_origin",
                        lambda pid: origins.get(pid, "gone"))
    return executed


def _ctx(tmp_path, asked=None):
    def approve(tool, detail):
        if asked is not None:
            asked.append(tool)
        return Approval.GRANTED  # approve-all: every prompt granted
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={}, approve=approve)


REFUSED = [
    # pkill / killall, any form (the incident's two commands first)
    "pkill -f build",
    "killall python3",
    "pkill my_test_server_zz",
    "pkill -n -f x",
    "killall -9 node",
    "/usr/bin/pkill -f gate",
    "pkill -u $USER",
    "killall -u someone",
    "pkill init",
    "killall systemd",
    "timeout 5 pkill -f x",
    "echo start && pkill -f x",
    "echo $(pkill -f x)",
    "sh -c 'pkill -f build'",
    "bash -lc \"killall python3\"",
    "eval pkill -f build",
    "busybox killall x",
    # pgrep | xargs kill
    "pgrep -f build | xargs kill",
    "pgrep python3 | xargs -r kill -9",
    "pgrep x | xargs -I{} kill {}",
    "ps aux | grep build | awk '{print $2}' | xargs kill",
    # kill of 1, 0, -1, negative PIDs
    "kill 1",
    "kill -9 1",
    "kill -TERM 1",
    "kill -s KILL 1",
    "kill 0",
    "kill -1",
    "kill -- -1",
    "kill -9 -1",
    "kill -9 -4242",
    "kill -- -4242",
    "kill -TERM -- -4242",
    # Wilbur itself
    "kill $PPID",
    "kill -15 ${PPID}",
    f"kill {os.getpid()}",
    f"kill -TERM {os.getppid()}",
    f"sleep 1; kill {os.getpid()}",
    f"true && timeout 5 kill -9 {os.getpid()}",
    # computed PIDs
    "kill $(pgrep -f build)",
    "kill -9 $(pidof python3)",
    "kill `pgrep build`",
    "kill $pid",
    "kill -9 ${PIDS}",
    # a running process the session did not start
    f"kill {FOREIGN_PID}",
    f"kill -9 {SESSION_PID} {FOREIGN_PID}",
    # systemctl / loginctl
    "systemctl --user stop gpu-queue.service",
    "systemctl --user kill api-budget",
    "systemctl --user stop 'gate-*'",
    "systemctl --user disable --now ram-guard",
    "systemctl stop nginx",
    "systemctl --user stop $(systemctl --user list-units --plain -q | awk '{print $1}')",
    "loginctl kill-user desktopuser1",
    "loginctl terminate-session 3",
    # docker / podman without exact names
    "docker kill $(docker ps -q)",
    "docker rm -f $(docker ps -aq)",
    "docker stop",
    "docker container stop -t 5",
    "docker ps -q | xargs docker stop",
    "docker rm 'web*'",
    "podman stop -a",
    "podman rm --all",
    "docker container prune -f",
    "docker system prune -af",
]


@pytest.mark.parametrize("command", REFUSED)
def test_refused(command):
    assert kill_refusal(command) is not None, command


ALLOWED = [
    f"kill {SESSION_PID}",
    f"kill -9 {SESSION_PID}",
    f"kill -TERM {SESSION_PID}",
    f"kill -s TERM {SESSION_PID}",
    f"kill {GONE_PID}",
    "kill $!",
    "sleep 30 & kill $!",
    "kill %1",
    "kill -l",
    "pgrep -f build",
    "echo kill 1",
    "echo pkill -f build",
    "grep -r 'kill 1' .",
    "grep -n killall README.md",
    "systemctl --user status gpu-queue",
    "systemctl --user is-active api-budget",
    f"systemctl --user stop wilbur-cmd-{os.getpid()}-3-abc123.scope",
    "docker stop my-container",
    "docker rm -f my-container other-container",
    "docker container kill -s TERM 4f2a9c1b7e3d",
    "docker ps -a",
    "sh -c 'echo hi'",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed(command):
    assert kill_refusal(command) is None, command


def test_registered_job_unit_may_be_stopped():
    killguard.register_session_unit("wilbur-job-unit-zz.scope")
    assert kill_refusal("systemctl --user stop wilbur-job-unit-zz") is None
    assert kill_refusal("systemctl --user stop wilbur-job-unit-zy") is not None


@pytest.mark.parametrize("command", [
    "pkill -f build",
    "killall python3",
    "pgrep -f build | xargs kill",
    f"kill {FOREIGN_PID}",
    "kill -1",
    "systemctl --user stop gpu-queue",
    "docker kill $(docker ps -q)",
])
def test_approve_all_still_refuses_run_bash(tmp_path, no_execution, command):
    asked: list[str] = []
    out = build_registry()["run_bash"].run({"command": command}, _ctx(tmp_path, asked))
    assert out.is_error and "Refused without running" in out.output
    assert asked == [], "refused before any approval prompt"
    assert no_execution == [], "nothing executed"


@pytest.mark.parametrize("command", ["pkill -f build", "killall python3",
                                     "kill $(pgrep build)"])
def test_approve_all_still_refuses_job_start(tmp_path, no_execution, command):
    from wilbur.tools.jobs import job_tools
    tools = {t.name: t for t in job_tools()}
    asked: list[str] = []
    out = tools["job_start"].run({"command": command, "name": "x"}, _ctx(tmp_path, asked))
    assert out.is_error and "Refused without running" in out.output
    assert asked == [] and no_execution == []


def test_allowed_exact_pid_reaches_fake_execution(tmp_path, no_execution):
    """Control: the exact-PID kill of a session process is not refused; it
    reaches execution, which here is the recording fake."""
    command = f"kill -TERM {SESSION_PID}"
    out = build_registry()["run_bash"].run({"command": command}, _ctx(tmp_path))
    assert not out.is_error, out.output
    assert no_execution == [command]


def test_old_guard_allowed_the_incident_commands():
    """Control: the 2.73.0 guard (frozen in tests/fixtures) passed both
    incident commands, so the tests above would have caught it."""
    path = Path(__file__).resolve().parent / "fixtures" / "killguard_2_73_0.py"
    spec = importlib.util.spec_from_file_location("killguard_2_73_0", path)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    for command in ("pkill -f build", "killall python3",
                    "pgrep -f build | xargs kill", "kill $(pgrep build)"):
        assert old.kill_refusal(command) is None
        assert kill_refusal(command) is not None


def test_process_origin_reads_proc_only():
    """The real ownership check (not the fixture's table): a missing PID is
    'gone', this process's parent is not the session's, a registered PID is."""
    assert _REAL_PROCESS_ORIGIN(2 ** 22 + 7) == "gone"
    assert _REAL_PROCESS_ORIGIN(os.getppid()) == "foreign"
    killguard.register_session_pid(os.getppid())
    try:
        assert _REAL_PROCESS_ORIGIN(os.getppid()) == "session"
    finally:
        killguard._SESSION_PIDS.discard(os.getppid())
