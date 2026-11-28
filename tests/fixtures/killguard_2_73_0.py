# Frozen copy of wilbur/tools/killguard.py at 2.73.0 (406fd30), the guard that
# allowed `pkill -f build` on 2026-11-27. Used only as a control in
# tests/test_shell_kill_guard.py; never imported by wilbur. Do not edit.
"""Refuse kill/pkill/killall aimed at PID 1, every process, or Wilbur itself.

Parity benchmark 2026-11-25 (`32_kill_pid_list`, 5 of 8 reps failed): under
approve-all (bench, `wilbur loop`) the model's own self-test SIGTERM'd Wilbur's
process (exit 143) and then tried PID 1. Approval modes cannot catch that, so
this guard runs in every mode, before approval, like the elevation guard.

What is refused (literal targets only; a `$(pgrep ...)` target cannot be
resolved before running and is left to approval):
- kill of 1, 0, -1 (init, the caller's group, every process the user owns);
- kill of Wilbur's PID, its parent's PID or its process group (PGID or -PGID),
  and `$PPID` (the shell's parent is Wilbur);
- pkill/killall whose pattern matches Wilbur's own process, init, or that
  selects by user (-u/--user, which includes Wilbur).
"""
from __future__ import annotations

import os
import re
import shlex

_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|\n(){}`]|\$\(")
_WRAPPERS = frozenset({"env", "command", "exec", "nohup", "time", "nice",
                       "ionice", "timeout", "stdbuf", "setsid", "xargs",
                       "builtin", "chronic", "unbuffer"})
_KILLERS = frozenset({"kill", "pkill", "killall"})
_ALWAYS_PROTECTED = {0: "0 (the shell's whole process group)",
                     1: "PID 1 (init)",
                     -1: "-1 (every process you own)"}
# pkill/killall options that take a value (the value is not a pattern)
_PKILL_VALUE_OPTS = frozenset({"-s", "--signal", "-g", "--pgroup", "-G", "--group",
                               "-P", "--parent", "-t", "--terminal", "-F", "--pidfile",
                               "--session", "--ns", "--nslist", "-O", "--older",
                               "-y", "--younger"})
_USER_OPTS = frozenset({"-u", "--user", "-U", "--uid"})
REFUSAL = (
    "Refused without running: `{cmd}` would signal {what}. Killing PID 1, every "
    "process (-1/0) or Wilbur's own process/group/parent ends this session. "
    "Signal only the specific child PIDs your command started (e.g. `kill $!` "
    "after starting it with `&`), or use a test fixture's own process."
)


def _own_identity() -> dict[int, str]:
    pid, ppid = os.getpid(), os.getppid()
    protected = dict(_ALWAYS_PROTECTED)
    protected[pid] = f"Wilbur's own process (PID {pid})"
    protected[ppid] = f"Wilbur's parent (PID {ppid})"
    try:
        pgid = os.getpgid(0)
    except OSError:
        pgid = None
    if pgid is not None:
        protected.setdefault(pgid, f"Wilbur's process group leader (PGID {pgid})")
        protected[-pgid] = f"Wilbur's process group (-{pgid})"
    return protected


def _own_names() -> list[str]:
    names: list[str] = []
    try:
        with open(f"/proc/{os.getpid()}/comm", encoding="utf-8") as fh:
            names.append(fh.read().strip())
    except OSError:
        pass
    try:
        with open(f"/proc/{os.getpid()}/cmdline", "rb") as fh:
            names.append(fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip())
    except OSError:
        pass
    return [n for n in names if n]


def _segment_tokens(segment: str) -> list[str]:
    """The tokens of one command segment from the kill program on, or []."""
    try:
        tokens = shlex.split(segment, comments=True)
    except ValueError:
        tokens = segment.split()
    index = 0
    while index < len(tokens):
        word = tokens[index]
        if "=" in word and word.split("=", 1)[0].isidentifier():
            index += 1
            continue
        name = os.path.basename(word)
        if name in _KILLERS:
            return [name] + tokens[index + 1:]
        if name not in _WRAPPERS:
            return []
        index += 1
        while index < len(tokens) and (tokens[index].startswith("-")
                                       or re.match(r"^[0-9.]+[smhd]?$", tokens[index])):
            index += 1
    return []


def _kill_targets(args: list[str]) -> list[str]:
    targets: list[str] = []
    index = 0
    signal_seen = False
    while index < len(args):
        arg = args[index]
        if arg == "--":
            targets.extend(args[index + 1:])
            break
        if arg in ("-l", "-L", "--list", "--table"):
            return []
        if not signal_seen and arg in ("-s", "-n", "--signal"):
            signal_seen = True
            index += 2
            continue
        if not signal_seen and arg.startswith("-") and len(arg) > 1:
            signal_seen = True  # -9, -TERM, -SIGKILL
            index += 1
            continue
        targets.append(arg)
        index += 1
    return targets


def _check_kill(args: list[str], protected: dict[int, str]) -> str | None:
    for target in _kill_targets(args):
        if target == "$PPID":
            return "Wilbur ($PPID is the shell's parent)"
        try:
            value = int(target)
        except ValueError:
            continue
        if value in protected:
            return protected[value]
    return None


def _pattern_hits(pattern: str, names: list[str], exact: bool) -> bool:
    for name in names:
        if exact:
            if os.path.basename(name.split(" ", 1)[0]) == pattern or name == pattern:
                return True
            continue
        try:
            if re.search(pattern, name):
                return True
        except re.error:
            if pattern in name:
                return True
    return False


def _check_pattern_killer(prog: str, args: list[str]) -> str | None:
    full = False
    patterns: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in _USER_OPTS or arg.startswith("--user="):
            return "every process of a user (that includes Wilbur)"
        if arg in ("-f", "--full"):
            full = True
        elif arg in _PKILL_VALUE_OPTS:
            index += 1
        elif arg.startswith("-") and len(arg) > 1:
            pass
        else:
            patterns.append(arg)
        index += 1
    own = _own_names()
    comm = own[:1]
    names = own if full else comm
    for pattern in patterns:
        if pattern in ("init", "systemd"):
            return f"PID 1 ({pattern})"
        if _pattern_hits(pattern, names, exact=(prog == "killall")):
            return f"Wilbur's own process (pattern {pattern!r} matches it)"
    return None


def kill_refusal(command: str) -> str | None:
    """The refusal message when `command` would kill a protected target."""
    protected = _own_identity()
    command = command.replace("${PPID}", "$PPID")  # braces would split the segment
    for segment in _SEGMENT_SPLIT.split(command):
        tokens = _segment_tokens(segment.strip())
        if not tokens:
            continue
        prog, args = tokens[0], tokens[1:]
        what = (_check_kill(args, protected) if prog == "kill"
                else _check_pattern_killer(prog, args))
        if what:
            return REFUSAL.format(cmd=segment.strip()[:120], what=what)
    return None
