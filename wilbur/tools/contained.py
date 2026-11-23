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
"""
from __future__ import annotations

import itertools
import os
import resource
import signal
import subprocess
import uuid
from dataclasses import dataclass
from functools import lru_cache

DEFAULT_MEMORY_MAX = "8G"
_counter = itertools.count()


@dataclass
class Completed:
    returncode: int
    stdout: str
    stderr: str


class Timeout(Exception):
    pass


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
        probe = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "--collect",
             "-p", "MemoryMax=64M", "--", "true"],
            capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _unit_name() -> str:
    return f"wilbur-cmd-{os.getpid()}-{next(_counter)}-{uuid.uuid4().hex[:6]}"


def wrap(argv: list[str], memory_max: str, unit: str | None) -> list[str]:
    if unit is None:
        return argv
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect",
            f"--unit={unit}", "-p", f"MemoryMax={memory_max}",
            "-p", "MemorySwapMax=0", "--", *argv]


def _rlimit_preexec(memory_max: str):
    limit = _bytes(memory_max)

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    return apply


def stop(proc: subprocess.Popen, unit: str | None) -> None:
    """Stop everything the command started: the scope by exact name, then the
    process group, SIGTERM then SIGKILL."""
    if unit is not None:
        subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", f"{unit}.scope"],
                       capture_output=True, timeout=10)
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
    return Completed(proc.returncode, out or "", err or "")


def shell(command: str, cwd: str, timeout: float,
          memory_max: str = DEFAULT_MEMORY_MAX) -> Completed:
    return run(["/bin/sh", "-c", command], cwd, timeout, memory_max)


def oom_note(returncode: int, memory_max: str, output: str = "") -> str:
    """A hint for the model when the command was killed (likely the cap)."""
    # Kernel cgroup OOM sends SIGKILL; systemd's scope OOMPolicy=stop then
    # SIGTERMs what is left, so /bin/sh itself often ends with -15. Under the
    # RLIMIT_AS fallback (no user systemd, e.g. CI) there is no signal: the
    # program fails its allocation instead (python: MemoryError, exit 1).
    if returncode in (-9, -15, 137, 143) or (returncode != 0 and "MemoryError" in output):
        return (f"\n[killed: the command was stopped, most likely by the {memory_max} "
                "memory cap. Look for an unbounded loop or allocation.]")
    return ""
