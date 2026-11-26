"""Recovery hints on failed tool calls, and the session's environment facts (2.58.0).

Measured on the 2.57.2 trace run (bench/results/trace-2572-2026-11-25, 10 tasks
x4, 45 failed calls): 15 run_bash calls ran `python -c ...` / `python -m
doctest` on a host that has only python3 (exit 127, `python: not found`), and
about 6 more went to the wrong place -- a `cd` into the parent of the task dir,
`write_file('/test_ranges.py')` at the filesystem root, grep on a dir that does
not exist. Each one cost a round before the model corrected itself.

Two halves:
- env_facts(): a few plain lines in the STATIC system prompt (project root, no
  `cd` needed, which interpreters exist), built once when the agent is made.
  Never rebuilt mid-session: the model resumes only from end-of-prompt
  checkpoints, so rewriting earlier prompt text forces a full re-read (2.40.1).
- *_hint(): one `[harness]` line appended to a FAILED tool result. Hints only:
  the command or path is never rewritten behind the model's back.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Callable

VENV_NAMES = (".venv", "venv", "env")

Which = Callable[[str], "str | None"]

# `/bin/sh: 1: python: not found` (dash), `bash: python: command not found`,
# `sh: python: not found`.
_NOT_FOUND = re.compile(
    r"(?:^|\n)[^\n:]*:\s*(?:\d+:\s*)?(?P<prog>[\w.+-]+): (?:command )?not found")
_CD_PREFIX = re.compile(r"^\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*(?:&&|;)")


def _project_venv(root: Path) -> Path | None:
    for name in VENV_NAMES:
        python = root / name / "bin" / "python"
        if python.exists():
            return python
    return None


def env_facts(cwd: str, which: Which | None = None) -> str:
    """The environment block for the static system prompt. Names no tool, so a
    restricted subagent is never told about one it lacks (test_directives)."""
    which = which or shutil.which
    root = Path(cwd)
    lines = ["Environment (probed at session start):",
             f"- Project root: {root}. Shell and test commands already run there: "
             "no `cd` needed, and use paths relative to it ('x.py', not '/x.py')."]
    has_python = which("python") is not None
    has_python3 = which("python3") is not None
    venv = _project_venv(root)
    if has_python3 and not has_python:
        lines.append("- Python: `python` is NOT on PATH -- use `python3` "
                     "(`python3 -m pip` for pip).")
    elif has_python:
        lines.append("- Python: `python` and `python3` are on PATH." if has_python3
                     else "- Python: `python` is on PATH (no `python3`).")
    else:
        lines.append("- Python: no python or python3 on PATH.")
    if venv is not None:
        lines.append(f"- Project venv: {venv} (prefer it for the project's own code and tests).")
    return "\n".join(lines)


def _alternative(prog: str, cwd: str, which: Which) -> str:
    venv = _project_venv(Path(cwd))
    if prog in ("python", "python2"):
        if venv is not None:
            return f"use `{venv}` (project venv) or `python3`"
        return "use `python3`" if which("python3") else ""
    if prog in ("pip", "pip3"):
        if venv is not None:
            return f"use `{venv} -m pip`"
        return "use `python3 -m pip`" if which("python3") else ""
    if which(prog + "3"):
        return f"use `{prog}3`"
    return ""


def missing_command_hint(output: str, cwd: str, which: Which | None = None) -> str:
    """A hint for an exit-127 `X: not found`, naming the alternative, else ''."""
    which = which or shutil.which
    if not output.startswith("exit 127"):
        return ""
    match = _NOT_FOUND.search(output)
    if not match:
        return ""
    prog = match.group("prog")
    alt = _alternative(prog, cwd, which)
    head = f"[harness] `{prog}` is not on PATH here"
    head += f"; {alt}." if alt else "."
    return head + " Retrying the same command will fail again."


def cd_hint(command: str, output: str, cwd: str) -> str:
    """A hint for a failed command whose leading `cd <dir>` is not the root."""
    if not output.startswith("exit "):
        return ""
    match = _CD_PREFIX.match(command or "")
    if not match:
        return ""
    raw = match.group(1).strip("\"'")
    target = Path(os.path.expanduser(raw))
    root = Path(cwd)
    if not target.is_absolute():
        target = root / target
    try:
        target_r, root_r = target.resolve(), root.resolve()
    except OSError:
        return ""
    if target_r == root_r:
        return ""
    if not target.is_dir():
        why = f"`cd {raw}`: no such directory"
    elif target_r in root_r.parents:
        why = f"`cd {raw}` went ABOVE the project root"
    else:
        return ""
    return f"[harness] {why}. run_bash already runs in {root}; drop the cd."


def _project_candidate(raw: str, root: Path, want_existing: bool) -> Path | None:
    """<root>/<basename> (or <root>/<path minus leading />) if it fits."""
    stripped = raw.lstrip("/")
    for cand in (root / stripped, root / Path(raw).name):
        if want_existing:
            if cand.exists():
                return cand
        elif cand.parent.is_dir() and os.access(cand.parent, os.W_OK):
            return cand
    return None


def outside_path_hint(raw: str, cwd: str, *, want_existing: bool) -> str:
    """A failed file op on an absolute path outside the project root: point
    at the project path that exists (read/edit) or is writable (write)."""
    if not raw:
        return ""
    path = Path(os.path.expanduser(raw))
    root = Path(cwd)
    if not path.is_absolute():
        return ""
    try:
        path.resolve().relative_to(root.resolve())
        return ""
    except ValueError:
        pass
    except OSError:
        return ""
    cand = _project_candidate(raw, root, want_existing)
    if cand is None:
        return ""
    rel = os.path.relpath(cand, root)
    return (f"[harness] {raw} is outside the project root {root}; "
            f"did you mean {cand} (relative: {rel!r})?")


_NEAREST_SCAN_LIMIT = 2000
_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


def _nearest_match(name: str, root: Path) -> Path | None:
    """A directory or file named `name` under root, bounded scan, else None."""
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP and not d.startswith("."))
        for entry in (*dirnames, *filenames):
            if entry == name:
                return Path(dirpath) / entry
        seen += 1
        if seen >= _NEAREST_SCAN_LIMIT:
            break
    return None


def missing_path_hint(raw: str, cwd: str) -> str:
    """grep/glob/list_files on a path that does not exist."""
    root = Path(cwd)
    line = f"[harness] {raw} does not exist; paths are relative to the project root {root}"
    name = Path(raw.rstrip("/")).name
    near = _nearest_match(name, root) if name and name not in (".", "..") else None
    if near is not None:
        return line + f"; nearest match: {os.path.relpath(near, root)!r}."
    return line + "; list_files('.') shows what is there."
