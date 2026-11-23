"""Model-issued commands are memory-capped and tree-killed (incident 2026-11-23:
an orphaned unbounded-allocation child of a timed-out run_bash hung pop-os)."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from wilbur.tools import contained
from wilbur.tools.base import Approval
from wilbur.tools.shell import Bash
from wilbur.tools.test_runner import RunTests

LEAK = "python3 -c 'x=[]\nwhile True: x.append(\" \"*10**6)'"


def _ctx(tmp_path: Path, memory_max: str = "200M"):
    config = SimpleNamespace(tool_memory_max=memory_max, denied_bash=[],
                             max_tool_output_chars=24000, max_tool_output_lines=800,
                             message_check_max_s=60.0)
    return SimpleNamespace(cwd=tmp_path, config=config, inbox=None, cancel=None,
                           approve=lambda *_: Approval.GRANTED)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return stat.exists() and stat.read_text().split()[2] != "Z"


def test_unbounded_allocation_is_killed_at_the_cap(tmp_path):
    started = time.monotonic()
    result = Bash().run({"command": LEAK, "timeout": 60}, _ctx(tmp_path))
    assert result.is_error
    assert "memory cap" in result.output
    assert time.monotonic() - started < 30, "the cap, not the timeout, must stop it"


def test_run_tests_leak_is_killed_at_the_cap(tmp_path):
    result = RunTests().run({"command": LEAK, "timeout": 60}, _ctx(tmp_path))
    assert result.is_error
    assert "memory cap" in result.output


def test_timeout_kills_detached_grandchild(tmp_path):
    """The incident shape: sh times out, its child must not survive it. setsid
    escapes the process group, so only the scope kill can catch it."""
    pidfile = tmp_path / "pid"
    command = f"setsid sh -c 'echo $$ > {pidfile}; exec sleep 300' & wait"
    result = Bash().run({"command": command, "timeout": 2}, _ctx(tmp_path, "1G"))
    assert "timed out" in result.output
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if not contained.scope_available():
        pytest.skip("no user systemd scope here; setsid escape needs it")
    assert not _alive(pid), "a detached child outlived the timed-out command"


def test_control_uncapped_leak_grows_past_the_cap(tmp_path):
    """Control (§17): prove the leak really exceeds 200M when nothing caps it,
    so the kill above is the cap and not the command failing on its own."""
    proc = subprocess.Popen(["python3", "-c", "x=[]\nwhile True: x.append(' '*10**6)"])
    try:
        peak = 0
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and peak < 300 * 1024:
            for line in Path(f"/proc/{proc.pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    peak = max(peak, int(line.split()[1]))
            time.sleep(0.05)
        assert peak >= 300 * 1024
    finally:
        proc.kill()
        proc.wait()


def test_normal_command_unaffected(tmp_path):
    result = Bash().run({"command": "echo ok; exit 3", "timeout": 10}, _ctx(tmp_path))
    assert result.is_error and "exit 3" in result.output and "ok" in result.output
    assert "memory cap" not in result.output


def test_rlimit_fallback_caps_without_systemd(tmp_path, monkeypatch):
    monkeypatch.setattr(contained, "scope_available", lambda: False)
    proc = contained.shell(LEAK, str(tmp_path), timeout=30, memory_max="300M")
    assert proc.returncode != 0
    assert "MemoryError" in proc.stderr


def test_rlimit_fallback_still_tells_the_model(tmp_path, monkeypatch):
    """CI has no user systemd: the leak dies of MemoryError (exit 1), not a signal,
    and the model must still be told it hit the cap (2.49.0 mirror CI failure)."""
    monkeypatch.setattr(contained, "scope_available", lambda: False)
    result = Bash().run({"command": LEAK, "timeout": 30}, _ctx(tmp_path))
    assert result.is_error and "memory cap" in result.output
