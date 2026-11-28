"""Refuse process-killing commands that can hit anything the session did not start.

Runs in every approval mode, before approval (approve-all included), for
run_bash and job_start alike (shell.command_preflight).

History:
- Parity benchmark 2026-11-25 (`32_kill_pid_list`): under approve-all the
  model's self-test SIGTERM'd Wilbur (exit 143) and then tried PID 1.
- Incident 2026-11-27 21:29:31: a test draft ran `pkill -f build` and
  `killall python3` through run_bash. The old guard refused only patterns that
  matched Wilbur itself, so both ran: they killed two gate runs and SIGTERMed
  the user's services (gpu-queue, api-budget, ram-guard, ...). A pattern or a
  name selects processes nobody can list before it runs, so it is refused.

Refused:
- `pkill` and `killall` in any form, and any kill fed from a pipe (`xargs kill`);
- `kill` of 1, 0, -1 or any negative PID (a process group), of Wilbur, its
  parent or its group (`$PPID` too), of a PID computed at run time
  (`$(...)`, backticks, `$VAR`), and of a running process the session did not
  start (not a descendant of Wilbur, not in one of its `wilbur-cmd-<pid>-*`
  scopes, not registered by the job manager);
- `systemctl [--user] stop|kill` (and `disable|mask --now`) of a unit the
  session did not start, `loginctl kill-*|terminate-*`;
- `docker|podman kill|rm|stop` without exact container names (none, `-a`,
  globs, substitutions), and `docker ... prune`;
- the same inside `sh -c '...'`/`bash -c` and `eval`.
Allowed: `kill <pid>` / `kill -9 <pid>` of a session process, `kill $!`,
`kill %1`, `kill -l`, and `pgrep` on its own.
"""
from __future__ import annotations

import os
import re
import shlex

_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|\n(){}`]|\$\(")
_PLAIN_SPLIT = re.compile(r"&&|\|\||[;&|\n()]")
_KEYWORDS = frozenset({"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until"})
_WRAPPERS = frozenset({"env", "command", "exec", "nohup", "time", "nice",
                       "ionice", "timeout", "stdbuf", "setsid", "xargs",
                       "builtin", "chronic", "unbuffer"})
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "busybox"})
_GUARDED = frozenset({"kill", "pkill", "killall", "systemctl", "loginctl",
                      "docker", "podman", "eval"}) | _SHELLS
_SUBST = "__WILBUR_SUBST__"
_INNER_SUBST = re.compile(r"\$\([^()]*\)|`[^`]*`")
_GLOB = re.compile(r"[*?\[\]]")

# PIDs and units the session started outside a wilbur-cmd scope (job manager).
_SESSION_PIDS: set[int] = set()
_SESSION_UNITS: set[str] = set()

REFUSAL = (
    "Refused without running: `{cmd}` would signal {what}. Wilbur kills only "
    "exact processes it started: `kill <pid>` of a PID your own command "
    "started (or `kill $!` right after starting it with `&`), or `job_stop <id>` "
    "for a background job. Patterns, names, process groups, PID 1/0/-1 and "
    "PIDs computed at run time are refused in every approval mode."
)


def register_session_pid(pid: int) -> None:
    _SESSION_PIDS.add(int(pid))


def register_session_unit(unit: str | None) -> None:
    if unit:
        _SESSION_UNITS.add(_unit_base(unit))


def _unit_base(unit: str) -> str:
    for suffix in (".service", ".scope"):
        if unit.endswith(suffix):
            return unit[: -len(suffix)]
    return unit


def _session_unit_prefix() -> str:
    return f"wilbur-cmd-{os.getpid()}-"


def _own_identity() -> dict[int, str]:
    pid, ppid = os.getpid(), os.getppid()
    protected = {0: "0 (the shell's whole process group)", 1: "PID 1 (init)"}
    protected[pid] = f"Wilbur's own process (PID {pid})"
    protected[ppid] = f"Wilbur's parent (PID {ppid})"
    try:
        pgid = os.getpgid(0)
    except OSError:
        pgid = None
    if pgid is not None:
        protected.setdefault(pgid, f"Wilbur's process group leader (PGID {pgid})")
    return protected


def _read_parent(pid: int) -> int | None:
    with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
        stat = fh.read()
    return int(stat.rsplit(")", 1)[1].split()[1])


def process_origin(pid: int) -> str:
    """'gone', 'session' or 'foreign' for a positive PID (reads /proc only)."""
    if pid in _SESSION_PIDS:
        return "session"
    if not os.path.exists(f"/proc/{pid}"):
        return "gone"
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8", errors="replace") as fh:
            if _session_unit_prefix() in fh.read():
                return "session"
        me, current, hops = os.getpid(), pid, 0
        while current > 1 and hops < 64:
            current = _read_parent(current)
            if current == me:
                return "session"
            hops += 1
    except (OSError, ValueError, IndexError):
        return "gone" if not os.path.exists(f"/proc/{pid}") else "foreign"
    return "foreign"


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return segment.split()


def _segment_program(segment: str) -> tuple[str, list[str], bool]:
    """(program, args, piped_through_xargs) for a guarded program, else ('', [], False)."""
    tokens = _tokens(segment)
    index, via_xargs = 0, False
    while index < len(tokens):
        word = tokens[index]
        if "=" in word and word.split("=", 1)[0].isidentifier():
            index += 1
            continue
        if word in _KEYWORDS:
            index += 1
            continue
        name = os.path.basename(word)
        if name in _GUARDED:
            return name, tokens[index + 1:], via_xargs
        if name not in _WRAPPERS:
            return "", [], False
        via_xargs = via_xargs or name == "xargs"
        index += 1
        while index < len(tokens) and (tokens[index].startswith("-")
                                       or re.match(r"^[0-9.]+[smhd]?$", tokens[index])
                                       or tokens[index] in ("{}", _SUBST)):
            index += 1
    return "", [], False


def _kill_targets(args: list[str]) -> list[str] | None:
    """The targets of `kill`, or None for `kill -l` (lists signals)."""
    targets: list[str] = []
    index, signal_seen = 0, False
    while index < len(args):
        arg = args[index]
        if arg == "--":
            targets.extend(args[index + 1:])
            break
        if arg in ("-l", "-L", "--list", "--table"):
            return None
        if not signal_seen and arg in ("-s", "-n", "--signal"):
            signal_seen = True
            index += 2
            continue
        if not signal_seen and arg.startswith("-") and len(arg) > 1 and not arg[1:].isdigit():
            signal_seen = True  # -TERM, -SIGKILL
            index += 1
            continue
        if not signal_seen and re.fullmatch(r"-\d+", arg) and index + 1 < len(args):
            signal_seen = True  # -9 followed by a target
            index += 1
            continue
        targets.append(arg)
        index += 1
    return targets


def _check_kill(args: list[str], via_xargs: bool) -> str | None:
    targets = _kill_targets(args)
    if targets is None:
        return None
    if via_xargs:
        return "PIDs read from a pipe (`... | xargs kill`), which cannot be checked first"
    protected = _own_identity()
    for target in targets:
        if target in ("$PPID", "${PPID}"):
            return "Wilbur ($PPID is the shell's parent)"
        if target in ("$!", "${!}") or target.startswith("%"):
            continue
        if _SUBST in target or "$" in target or "`" in target:
            return f"a PID computed at run time ({target!r})"
        try:
            value = int(target)
        except ValueError:
            continue
        if value < 0:
            return f"process group {value} (every process in it)"
        if value in protected:
            return protected[value]
        if process_origin(value) == "foreign":
            return f"PID {value}, a process this session did not start"
    return None


def _check_systemctl(args: list[str]) -> str | None:
    value_opts = {"-s", "--signal", "-H", "--host", "-M", "--machine",
                  "--kill-whom", "--kill-value", "-p", "--property", "-t", "--type"}
    words: list[str] = []
    now = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in value_opts:
            index += 2
            continue
        if arg == "--now":
            now = True
        elif not arg.startswith("-"):
            words.append(arg)
        index += 1
    if not words:
        return None
    verb, units = words[0], words[1:]
    if verb not in ("stop", "kill") and not (verb in ("disable", "mask") and now):
        return None
    for unit in units:
        if _SUBST in unit or "$" in unit or _GLOB.search(unit):
            return f"units matched at run time ({unit!r})"
        base = _unit_base(unit)
        if not (base.startswith(_session_unit_prefix()) or base in _SESSION_UNITS):
            return f"unit {unit!r}, which this session did not start"
    return None


def _check_loginctl(args: list[str]) -> str | None:
    for arg in args:
        if arg.startswith("kill-") or arg.startswith("terminate-"):
            return f"every process of a login session or user (`loginctl {arg}`)"
    return None


def _check_docker(prog: str, args: list[str], via_xargs: bool) -> str | None:
    global_value_opts = {"--context", "-c", "-H", "--host", "--config", "-l", "--log-level"}
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in global_value_opts else 1
    rest = args[index:]
    if rest[:1] == ["container"] or rest[:1] == ["system"] or rest[:1] == ["image"]:
        rest = rest[1:]
    if not rest:
        return None
    verb, rest = rest[0], rest[1:]
    if verb == "prune":
        return f"every stopped container or unused object (`{prog} ... prune`)"
    if verb not in ("kill", "rm", "stop"):
        return None
    if via_xargs:
        return f"containers read from a pipe (`xargs {prog} {verb}`)"
    value_opts = {"-t", "--time", "--timeout", "-s", "--signal"}
    names: list[str] = []
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg in ("-a", "--all"):
            return f"every container (`{prog} {verb} {arg}`)"
        if arg in value_opts:
            index += 2
            continue
        if not arg.startswith("-"):
            names.append(arg)
        index += 1
    if not names:
        return f"containers chosen at run time (`{prog} {verb}` needs exact container names)"
    for name in names:
        if _SUBST in name or "$" in name or "`" in name or _GLOB.search(name):
            return f"containers chosen at run time ({name!r}; give exact names)"
    return None


def _check_segment(segment: str, depth: int) -> str | None:
    prog, args, via_xargs = _segment_program(segment)
    if not prog:
        return None
    if prog == "kill":
        return _check_kill(args, via_xargs)
    if prog in ("pkill", "killall"):
        return f"every process whose name matches ({prog} selects by pattern or name)"
    if prog == "systemctl":
        return _check_systemctl(args)
    if prog == "loginctl":
        return _check_loginctl(args)
    if prog in ("docker", "podman"):
        return _check_docker(prog, args, via_xargs)
    if prog == "eval":
        return _refusal_what(" ".join(args), depth + 1)
    if prog in _SHELLS:
        if prog == "busybox" and args:
            return _check_segment(" ".join(shlex.quote(a) for a in args), depth + 1)
        for index, arg in enumerate(args):
            if arg.startswith("-") and "c" in arg[1:] and index + 1 < len(args):
                return _refusal_what(args[index + 1], depth + 1)
    return None


def _refusal_what(command: str, depth: int = 0) -> str | None:
    if depth > 4:
        return "a command nested too deeply to check"
    # Pass 1: each substitution's own content (`echo $(pkill x)`).
    for segment in _SEGMENT_SPLIT.split(command):
        what = _check_segment(segment.strip(), depth)
        if what:
            return what
    # Pass 2: substitutions replaced by a marker, so `kill $(pgrep x)` keeps
    # its (computed) target.
    flattened, previous = command, None
    while previous != flattened:
        previous, flattened = flattened, _INNER_SUBST.sub(f" {_SUBST} ", flattened)
    for segment in _PLAIN_SPLIT.split(flattened):
        what = _check_segment(segment.strip(), depth)
        if what:
            return what
    return None


def _segment_for_message(command: str) -> str:
    return " ".join(command.split())[:120]


def kill_refusal(command: str) -> str | None:
    """The refusal message when `command` could kill something the session
    did not start, else None. Pure parse and decide (it reads /proc only)."""
    what = _refusal_what(command)
    if what:
        return REFUSAL.format(cmd=_segment_for_message(command), what=what)
    return None
