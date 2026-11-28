"""Diagnostics on edit (BACKLOG `tool-diagnostics-on-edit`).

After write_file / edit_file (and any future patch tool, via `annotate`) the
matching fast checkers run on the touched file and their findings are
appended to the tool result, so an error surfaces on the edit instead of one
`run_tests` round later.

Rules the tests pin down:
- `diagnostics_on_edit = False` returns the message unchanged, byte for byte.
- A checker that is not installed is never silent: the result carries one
  "skipped (not installed)" line naming it.
- Every checker has a wall-clock timeout; a timeout says so and never blocks
  the edit. The whole process group is killed, so a wrapper (`tb`) child
  does not outlive it.
- Output is capped (lines and chars) so a noisy checker cannot flood context.
- A clean file with every checker present appends nothing.

`diagnostics_commands` in the config overrides or supplies a checker's argv
prefix, e.g. {"shellcheck": ["tb", "shellcheck"]} to
run one from the toolbox container instead of a host install (§20b).
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

PY_SUFFIXES = {".py", ".pyi"}
SHELL_SUFFIXES = {".sh", ".bash"}
C_SUFFIXES = {".c"}
CXX_SUFFIXES = {".cc", ".cpp", ".cxx", ".c++"}
GD_SUFFIXES = {".gd"}
_SHELL_SHEBANG = re.compile(r"^#!.*\b(sh|bash|dash|ksh)\b")


@dataclass(frozen=True)
class Checker:
    name: str
    # Candidate argv prefixes tried in order; the first whose program resolves
    # is used. The file path is appended.
    candidates: tuple[tuple[str, ...], ...]
    # Lines kept from the output (None = every non-empty line).
    keep: re.Pattern[str] | None = None


CHECKERS: dict[str, Checker] = {
    "ruff": Checker("ruff", (("ruff", "check", "--output-format=concise", "--no-cache",
                              "--quiet"),)),
    "pyright": Checker("pyright", (("pyright",),),
                       keep=re.compile(r" - (error|warning)")),
    "shellcheck": Checker("shellcheck", (("shellcheck", "-f", "gcc"),)),
    # clang-tidy first; else a syntax-only compile of the one translation unit.
    "c": Checker("c", (("clang-tidy", "--quiet"), ("cc", "-fsyntax-only"),
                       ("gcc", "-fsyntax-only"), ("clang", "-fsyntax-only"))),
    "c++": Checker("c++", (("clang-tidy", "--quiet"), ("c++", "-fsyntax-only"),
                           ("g++", "-fsyntax-only"), ("clang++", "-fsyntax-only"))),
    "gdlint": Checker("gdlint", (("gdlint",),)),
}


def checkers_for(path: Path, text: str | None = None) -> list[str]:
    suffix = path.suffix.lower()
    if suffix in PY_SUFFIXES:
        return ["python-syntax", "ruff", "pyright"]
    if suffix in SHELL_SUFFIXES:
        return ["shellcheck"]
    if suffix in C_SUFFIXES:
        return ["c"]
    if suffix in CXX_SUFFIXES:
        return ["c++"]
    if suffix in GD_SUFFIXES:
        return ["gdlint"]
    if not suffix and text is not None:
        first = text.split("\n", 1)[0]
        if _SHELL_SHEBANG.match(first):
            return ["shellcheck"]
        if first.startswith("#!") and "python" in first:
            return ["python-syntax", "ruff", "pyright"]
    return []


def _which(program: str) -> str | None:
    """PATH lookup, then the running interpreter's bin dir (a venv's ruff)."""
    if os.sep in program:
        return program if os.access(program, os.X_OK) else None
    found = shutil.which(program)
    if found:
        return found
    local = Path(sys.executable).parent / program
    return str(local) if os.access(local, os.X_OK) else None


def resolve(name: str, overrides: dict[str, list[str]] | None) -> list[str] | None:
    if overrides and overrides.get(name):
        argv = list(overrides[name])
        exe = _which(argv[0])
        return [exe, *argv[1:]] if exe else None
    for candidate in CHECKERS[name].candidates:
        exe = _which(candidate[0])
        if exe:
            argv = [exe, *candidate[1:]]
            # clang-tidy wants "--" after the file so it does not hunt for a
            # compile database; appended by _run_external.
            return argv
    return None


def _python_syntax(path: Path, text: str) -> list[str]:
    """In-process compile(): always available, catches syntax errors even
    when ruff/pyright are not installed."""
    try:
        compile(text, str(path), "exec", dont_inherit=True)
    except SyntaxError as exc:
        where = f"{exc.lineno}:{exc.offset or 0}" if exc.lineno else "?"
        return [f"{path.name}:{where}: SyntaxError: {exc.msg}"]
    except ValueError as exc:  # e.g. null bytes
        return [f"{path.name}: {exc}"]
    return []


def _run_external(argv: list[str], path: Path, cwd: str, timeout_s: float) -> tuple[str, list[str]]:
    """Returns (status, lines): status is "ok", "findings", "timeout" or "error"."""
    full = [*argv, str(path)]
    if Path(argv[0]).name == "clang-tidy":
        full.append("--")
    try:
        proc = subprocess.Popen(full, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, errors="replace", start_new_session=True)
    except OSError as exc:
        return "error", [str(exc)]
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.communicate()
        return "timeout", []
    lines = [line.rstrip() for line in out.splitlines() if line.strip()]
    if proc.returncode == 0:
        return "ok", []
    if proc.returncode == 1 or Path(argv[0]).name in ("cc", "gcc", "clang", "c++", "g++",
                                                       "clang++", "clang-tidy"):
        return "findings", lines
    return "error", [f"exit {proc.returncode}", *lines[:3]]


def diagnose(path: Path, cwd: str, config: Any, text: str | None = None,
             runner: Callable[..., tuple[str, list[str]]] | None = None) -> str:
    """The diagnostics block for one file ("" when clean and nothing skipped)."""
    if text is None:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return ""
    names = checkers_for(path, text)
    if not names:
        return ""
    timeout_s = float(getattr(config, "diagnostics_timeout_s", 10.0))
    overrides = getattr(config, "diagnostics_commands", None) or {}
    disabled = set(getattr(config, "diagnostics_disabled", None) or [])
    run = runner or _run_external
    display = str(path)
    try:
        display = os.path.relpath(path, cwd)
    except ValueError:
        pass
    findings: list[str] = []
    skipped: list[str] = []
    notes: list[str] = []
    for name in names:
        if name in disabled:
            continue
        if name == "python-syntax":
            errs = _python_syntax(path, text)
            findings += [f"python-syntax: {line}" for line in errs]
            continue
        argv = resolve(name, overrides)
        if argv is None:
            skipped.append(name)
            continue
        status, lines = run(argv, path, cwd, timeout_s)
        keep = CHECKERS[name].keep
        if status == "timeout":
            notes.append(f"{name}: timed out after {timeout_s:g}s (no result)")
        elif status == "error":
            notes.append(f"{name}: could not run ({'; '.join(lines)[:200]})")
        elif status == "findings":
            kept = [line for line in lines if keep is None or keep.search(line)]
            kept = [line.replace(str(path), display).strip() for line in kept]
            findings += [f"{name}: {line}" for line in kept]
    if not (findings or skipped or notes):
        return ""
    max_lines = int(getattr(config, "diagnostics_max_lines", 40))
    max_chars = int(getattr(config, "diagnostics_max_chars", 4000))
    body = findings
    if len(body) > max_lines:
        body = body[:max_lines] + [f"... {len(findings) - max_lines} more finding(s) not shown"]
    out = [f"[diagnostics: {display}: {len(findings)} finding(s)]" if findings
           else f"[diagnostics: {display}: no findings]"]
    out += body + notes
    if skipped:
        out.append(f"skipped (not installed): {', '.join(skipped)}")
    block = "\n".join(out)
    if len(block) > max_chars:
        block = block[:max_chars] + "\n... [diagnostics output capped]"
    return block


def annotate(message: str, paths: Iterable[Path], cwd: str, config: Any,
             runner: Callable[..., tuple[str, list[str]]] | None = None) -> str:
    """Append diagnostics for the touched paths to a tool result message.
    Switch off -> the message is returned unchanged (the control)."""
    if not getattr(config, "diagnostics_on_edit", False):
        return message
    blocks = [b for b in (diagnose(Path(p), cwd, config, runner=runner) for p in paths) if b]
    return message + "\n\n" + "\n\n".join(blocks) if blocks else message
