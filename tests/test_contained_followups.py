"""contained-commands-followups (2.61.0): fallback virtual cap, honest OOM hint,
stop() without systemctl."""
from __future__ import annotations

import subprocess
import sys

import pytest

from wilbur.tools import contained

# Reserve 3x the cap of address space without touching it (large VSZ, tiny RSS),
# the shape of a Go/JVM binary.
BIG_VSZ = (f"{sys.executable} -c \"import mmap; m=mmap.mmap(-1, 3*300*1024*1024); "
           "m[0:1]=b'x'; print('reserved')\"")


def test_fallback_allows_large_vsz_small_rss(tmp_path, monkeypatch):
    monkeypatch.setattr(contained, "scope_available", lambda: False)
    proc = contained.shell(BIG_VSZ, str(tmp_path), timeout=30, memory_max="300M")
    assert proc.returncode == 0, proc.stderr
    assert "reserved" in proc.stdout


def test_fallback_factor_control(tmp_path, monkeypatch):
    """Control: at factor 1 (the 2.49.0 cap) the same program fails, so the
    test above measures the higher virtual cap."""
    monkeypatch.setattr(contained, "scope_available", lambda: False)
    monkeypatch.setattr(contained, "FALLBACK_AS_FACTOR", 1)
    proc = contained.shell(BIG_VSZ, str(tmp_path), timeout=30, memory_max="300M")
    assert proc.returncode != 0


def test_scoped_sigterm_exit_gets_no_cap_hint(tmp_path):
    if not contained.scope_available():
        pytest.skip("no user systemd scope here")
    # No kill-family shell command in tests (2026-11-27 incident): the process
    # signals only its own PID through the os API.
    self_term = f"{sys.executable} -c 'import os, signal; os.kill(os.getpid(), signal.SIGTERM)'"
    proc = contained.shell(f"exec {self_term}", str(tmp_path), timeout=10, memory_max="200M")
    assert proc.returncode in (-15, 143)
    assert proc.oom_killed is False
    assert contained.oom_note(proc.returncode, "200M", "", proc.oom_killed) == ""


def test_scoped_real_oom_is_reported(tmp_path):
    if not contained.scope_available():
        pytest.skip("no user systemd scope here")
    leak = f"{sys.executable} -c 'x=[]\nwhile True: x.append(\" \"*10**6)'"
    proc = contained.shell(leak, str(tmp_path), timeout=60, memory_max="150M")
    assert proc.oom_killed is True
    assert "memory cap" in contained.oom_note(proc.returncode, "150M", "", proc.oom_killed)


def test_fallback_sigterm_gets_no_hint():
    assert contained.oom_note(-15, "8G", "") == ""
    assert contained.oom_note(143, "8G", "") == ""


def test_suite_asserting_memoryerror_gets_no_hint():
    out = "FAILED tests/test_x.py::test_raises_memoryerror\nE   MemoryError: boom\n1 failed"
    assert contained.oom_note(1, "8G", out) == ""


def test_uncaught_memoryerror_still_hinted_control():
    out = "Traceback (most recent call last):\n  File \"x.py\", line 2\nMemoryError\n"
    assert "memory cap" in contained.oom_note(1, "8G", out)


def test_stop_survives_missing_systemctl(monkeypatch):
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    real_run = subprocess.run

    def no_systemctl(argv, *a, **kw):
        if argv and argv[0] == "systemctl":
            raise FileNotFoundError("systemctl")
        return real_run(argv, *a, **kw)
    monkeypatch.setattr(contained.subprocess, "run", no_systemctl)
    contained.stop(proc, "wilbur-cmd-test-missing")
    assert proc.poll() is not None
