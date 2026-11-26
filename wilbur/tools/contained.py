"""Run a model-issued command inside a memory-capped, killable container.

Why (incident 2026-11-23 13:52-14:12): during the Wilbur A/B bench, a model
wrote a CSV parser whose loop (`while i <= len(line)`) appended "" forever
without advancing. The bash tool ran it with `subprocess.run(shell=True,
timeout=...)`. On timeout that kills only /bin/sh; the python child was
orphaned and kept allocating until RAM (61 GiB) and all 28 GiB of swap were
gone. The kernel OOM killer never fired (zram livelock) and the owner had to
power-cycle pop-os.

So every command now runs:
  * in its own transient systemd user scope with MemoryMax and
    MemorySwapMax=0, so the kernel OOM-kills it inside the scope the moment it
    passes the cap, whatever else is running on the box;
  * in its own session, and on timeout the WHOLE scope is stopped by its exact
    unit name (catches children that called setsid), then the process group.
Where a user systemd manager with the memory controller is not available
(container, CI), it falls back to RLIMIT_AS plus the process-group kill.
RLIMIT_AS caps VIRTUAL memory, not RSS, so the fallback cap is
FALLBACK_AS_FACTOR x memory_max (32G for the 8G default): Go and JVM
binaries reserve far more address space than they touch and failed at 8G.
AddressSanitizer reserves terabytes of shadow memory and cannot run under any
RLIMIT_AS at all; run ASan builds where a user systemd scope is available.
"""
from __future__ import annotations

import itertools
import os
import re
import resource
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache

DEFAULT_MEMORY_MAX = "8G"
FALLBACK_AS_FACTOR = 4
_counter = itertools.count()


@dataclass
class Completed:
    returncode: int
    stdout: str
    stderr: str
    # Scoped runs: True/False from the scope's systemd Result (oom-kill or
    # not). None when there was no scope (fallback) or it could not be read.
    oom_killed: bool | None = None


class Timeout(Exception):
    pass


def timeout_hint(timeout: int) -> str:
    """2.52.1: a model that hits the timeout tends to resend the identical call until the
    tool-failure breaker stops it (seen 2026-11-24, 3x run_bash at timeout=60 on a slow suite)."""
    return (f"Retrying the same call will time out again: raise `timeout` (it was {timeout}s; "
            f"a full test suite may need 600), narrow the command to fewer tests, or run it in "
            f"the background and poll.")


def _bytes(size: str) -> int:
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    size = str(size).strip().upper()
    if size and size[-1] in units:
        return int(float(size[:-1]) * units[size[-1]])
    return int(size)


@lru_cache(maxsize=1)
def scope_available() -> bool:
    """True when `systemd-run --user --scope` can apply a memory cap here."""
    if os.environ.get("WILBUR_NO_SCOPE") == "1":
        return False
    try:
        # Probe with exactly the argv real commands get (wrap): a probe without
        # MemorySwapMax passed on hosts whose user manager rejects it, and then
        # every wrapped command failed (gates red in a clean CI).
        probe = subprocess.run(
            wrap(["true"], "64M", _unit_name()),
            capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _unit_name() -> str:
    return f"wilbur-cmd-{os.getpid()}-{next(_counter)}-{uuid.uuid4().hex[:6]}"


def wrap(argv: list[str], memory_max: str, unit: str | None) -> list[str]:
    if unit is None:
        return argv
    # --expand-environment=no: systemd-run documents that --scope will start
    # expanding ${VAR}/$$ in the command by default, as newer releases do.
    # The public mirror's CI (GitHub runner) turned `echo $$` into `$` and
    # was red from 2.44.1 to 2.54.0. A host whose systemd-run predates the
    # option (< 246) fails the scope_available() probe and falls back to RLIMIT.
    # No --collect (2.61.0): a scope the kernel OOM-killed stays loaded as
    # failed with Result=oom-kill until scope_oom_killed() reads it and
    # resets it; a scope that ends cleanly is unloaded on its own.
    return ["systemd-run", "--user", "--scope", "--quiet",
            "--expand-environment=no",
            f"--unit={unit}", "-p", f"MemoryMax={memory_max}",
            "-p", "MemorySwapMax=0", "--", *argv]


def _rlimit_preexec(memory_max: str):
    limit = _bytes(memory_max) * FALLBACK_AS_FACTOR

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    return apply


def stop(proc: subprocess.Popen, unit: str | None) -> None:
    """Stop everything the command started: the scope by exact name, then the
    process group, SIGTERM then SIGKILL."""
    if unit is not None:
        try:
            subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", f"{unit}.scope"],
                           capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass  # systemctl missing or hung: the process-group kill below still runs
        _reset_failed(unit)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue


def _reset_failed(unit: str) -> None:
    try:
        subprocess.run(["systemctl", "--user", "reset-failed", f"{unit}.scope"],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def scope_oom_killed(unit: str, returncode: int) -> bool | None:
    """Ask systemd whether the kernel OOM-killed this command's scope, then
    collect the unit. None when the answer could not be read."""
    if returncode == 0:
        return False
    result = None
    for _ in range(10):  # systemd may see the cgroup OOM event a moment after waitpid
        try:
            out = subprocess.run(
                ["systemctl", "--user", "show", "-p", "Result", "--value", f"{unit}.scope"],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        result = out.stdout.strip()
        if result == "oom-kill" or returncode not in (-9, 137):
            break
        time.sleep(0.1)
    _reset_failed(unit)
    if result == "oom-kill":
        return True
    return False if result else None


def popen(argv: list[str], cwd: str, memory_max: str = DEFAULT_MEMORY_MAX,
          **kwargs) -> tuple[subprocess.Popen, str | None]:
    unit = _unit_name() if scope_available() else None
    preexec = None if unit is not None else _rlimit_preexec(memory_max)
    proc = subprocess.Popen(wrap(argv, memory_max, unit), cwd=cwd, start_new_session=True,
                            stdin=subprocess.DEVNULL, preexec_fn=preexec, **kwargs)
    return proc, unit


def run(argv: list[str], cwd: str, timeout: float,
        memory_max: str = DEFAULT_MEMORY_MAX) -> Completed:
    """subprocess.run(capture_output=True, text=True) with a memory cap and a
    timeout that kills the whole command tree. Raises Timeout."""
    proc, unit = popen(argv, cwd, memory_max, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, errors="replace")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop(proc, unit)
        raise Timeout() from None
    except BaseException:
        # Ctrl-C: the command runs in its own session, so SIGINT never reached it.
        stop(proc, unit)
        raise
    oom = scope_oom_killed(unit, proc.returncode) if unit is not None else None
    return Completed(proc.returncode, out or "", err or "", oom)


def shell(command: str, cwd: str, timeout: float,
          memory_max: str = DEFAULT_MEMORY_MAX) -> Completed:
    return run(["/bin/sh", "-c", command], cwd, timeout, memory_max)


_UNCAUGHT_MEMORY_ERROR = re.compile(r"(?m)^MemoryError\b")


def oom_note(returncode: int, memory_max: str, output: str = "",
             oom_killed: bool | None = None) -> str:
    """A hint for the model when the command was stopped by the memory cap.

    Scoped runs pass `oom_killed` from the scope's own systemd Result, so a
    command that exits on its own SIGTERM gets no cap hint (2.61.0). Without
    a scope (RLIMIT_AS fallback, e.g. CI) there is no signal: the program
    fails its allocation instead, so the hint fires on SIGKILL or on an
    UNCAUGHT MemoryError (a traceback's last line), not on a test suite that
    merely mentions or asserts MemoryError."""
    if oom_killed is not None:
        hit = oom_killed
    else:
        hit = returncode in (-9, 137) or (
            returncode != 0 and bool(_UNCAUGHT_MEMORY_ERROR.search(output)))
    if hit:
        return (f"\n[killed: the command was stopped, most likely by the {memory_max} "
                "memory cap. Look for an unbounded loop or allocation.]")
    return ""
