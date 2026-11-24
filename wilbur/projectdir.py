"""Project-directory awareness (2.52.0).

Three things that exist because the laptop launcher starts `wilbur` in $HOME
(owner session 2026-11-23): the model ran `./venv/bin/python -m pytest` in
$HOME, got ~/venv (no pytest), and the project's WILBUR.md
never loaded because instructions are read only from the cwd.

- last project: the git root Wilbur last worked in, recorded in
  ~/.local/state/wilbur/last-project; an interactive start in $HOME returns there.
- module hint: a `No module named X` result names the interpreter, the cwd and
  any project venvs, so the model stops guessing at `./venv`.
- global instructions: ~/.config/wilbur/WILBUR.md, loaded on every session.

Paths are read from the environment at call time so tests redirect them.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

GLOBAL_INSTRUCTIONS_MAX = 6000
VENV_NAMES = (".venv", "venv", "env")


def last_project_path() -> Path:
    return Path(os.environ.get("WILBUR_LAST_PROJECT",
                               Path.home() / ".local" / "state" / "wilbur" / "last-project"))


def global_instructions_path() -> Path:
    return Path(os.environ.get("WILBUR_GLOBAL_INSTRUCTIONS",
                               Path.home() / ".config" / "wilbur" / "WILBUR.md"))


def git_root(path: str | os.PathLike) -> Path | None:
    """The nearest ancestor holding .git (dir or worktree file), or None."""
    try:
        here = Path(path).expanduser().resolve()
    except OSError:
        return None
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def record_last_project(path: str | os.PathLike) -> Path | None:
    """Record the git root containing `path`. $HOME itself is never a project:
    recording it would make the auto-switch a no-op loop."""
    root = git_root(path)
    if root is None or root == Path.home().resolve():
        return None
    target = last_project_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(str(root) + "\n")
        os.replace(tmp, target)
    except OSError:
        return None
    return root


def last_project() -> Path | None:
    """The recorded dir, only if it still exists."""
    try:
        text = last_project_path().read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    path = Path(text)
    return path if path.is_absolute() and path.is_dir() else None


def startup_dir(cwd: str, *, explicit_dir: bool, here: bool) -> Path | None:
    """Where an interactive session should move to, or None to stay.

    Only a start in $HOME with no -C and no --here switches."""
    if explicit_dir or here:
        return None
    try:
        if Path(cwd).resolve() != Path.home().resolve():
            return None
    except OSError:
        return None
    return last_project()


_CD_PREFIX = re.compile(r"^\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*(?:&&|;)")


def cd_prefix_dir(command: str) -> Path | None:
    """The absolute dir of a leading `cd <dir> &&` / `cd <dir>;`, else None."""
    match = _CD_PREFIX.match(command or "")
    if not match:
        return None
    raw = match.group(1).strip("\"'")
    path = Path(os.path.expanduser(raw))
    return path if path.is_absolute() else None


def note_command(command: str) -> Path | None:
    """Record the git root of an absolute `cd` prefix, if it is in a repo."""
    target = cd_prefix_dir(command)
    if target is None or not target.is_dir():
        return None
    return record_last_project(target)


_NO_MODULE = re.compile(
    r"(?:^|\n)\s*(?:(?P<interp>\S+): )?No module named '?(?P<mod>[A-Za-z_][\w.]*)'?")
_PYTHON_IN_CMD = re.compile(r"(?:^|[\s;&|(])(?P<py>[^\s;&|()]*python[\d.]*)(?=\s|$)")


def _find_venvs(dirs: list[Path]) -> list[Path]:
    found: list[Path] = []
    for base in dirs:
        for name in VENV_NAMES:
            python = base / name / "bin" / "python"
            if python.exists() and python not in found:
                found.append(python)
    return found


def module_hint(command: str, output: str, cwd: str) -> str:
    """A <=3-line `[harness]` hint for a `No module named X` result, else ''."""
    match = _NO_MODULE.search(output or "")
    if not match:
        return ""
    module = match.group("mod").split(".")[0]
    run_dir = cd_prefix_dir(command) or Path(cwd)
    interp = match.group("interp")
    if not interp or "python" not in interp:
        cmd_match = _PYTHON_IN_CMD.search(command or "")
        interp = cmd_match.group("py") if cmd_match else "unknown interpreter"
    if interp.startswith("."):
        interp = os.path.normpath(str(run_dir / interp))
    dirs = [run_dir]
    root = git_root(run_dir)
    if root is not None and root not in dirs:
        dirs.append(root)
    venvs = _find_venvs(dirs)
    lines = [f"[harness] No module named {module!r}: the command ran {interp} in {run_dir}."]
    if venvs:
        best = venvs[0]
        lines.append("[harness] project venv(s) here: " + ", ".join(str(v) for v in venvs[:3]))
        lines.append(f"[harness] run it with {best} (e.g. `{best} -m {module}`), "
                     f"or install it there: `{best} -m pip install {module}`.")
    else:
        lines.append(f"[harness] no project venv ({', '.join(VENV_NAMES)}) under {run_dir}; "
                     "check this is the project directory (`cd <project> && ...`) before installing anything.")
    return "\n".join(lines)


def global_instructions() -> str:
    """The global ~/.config/wilbur/WILBUR.md block, or ''."""
    path = global_instructions_path()
    try:
        body = path.read_text(errors="replace")[:GLOBAL_INSTRUCTIONS_MAX].strip()
    except OSError:
        return ""
    return f"Global instructions from {path}:\n\n{body}" if body else ""
