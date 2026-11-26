"""Glob and grep, shelling out to fd/rg when present and falling back cleanly."""
from __future__ import annotations

import os
import re
import shutil
import time
import subprocess
from pathlib import Path
from typing import Any

from . import hints
from .base import ToolContext, ToolResult, clamp

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             "dist", "build", ".mypy_cache", ".pytest_cache"}

# 2.60 glob-unbounded-walk: a `**` glob from `/` once walked a 14TB backup
# drive in D state for minutes. The walk now prunes SKIP_DIRS and other mount
# points before entering them, and stops at a time budget or a match cap.
GLOB_TIME_BUDGET_S = 20.0
GLOB_MAX_MATCHES = 5000


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a Path.glob-style pattern (`**`, `*`, `?`, `[...]`) into a
    regex over a '/'-joined path relative to the root."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape("["))
                i += 1
            else:
                body = pattern[i + 1:end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def bounded_glob(root: Path, pattern: str, *, time_budget_s: float = GLOB_TIME_BUDGET_S,
                 max_matches: int = GLOB_MAX_MATCHES,
                 clock: Any = time.monotonic) -> tuple[list[Path], str]:
    """Walk root top-down, never entering SKIP_DIRS or another filesystem.
    Returns (files matched, reason the walk was cut short or "")."""
    pattern = pattern.lstrip("/")
    regex = _glob_regex(pattern)
    max_depth = None if "**" in pattern else pattern.count("/")
    try:
        root_dev = os.stat(root).st_dev
    except OSError:
        return [], ""
    deadline = clock() + time_budget_s
    matches: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        depth = rel_dir.count("/")
        kept = []
        for d in dirnames:
            if d in SKIP_DIRS or (max_depth is not None and depth >= max_depth):
                continue
            full = os.path.join(dirpath, d)
            try:
                if os.path.islink(full) or os.stat(full).st_dev != root_dev:
                    continue
            except OSError:
                continue
            kept.append(d)
        dirnames[:] = kept  # prune in place: os.walk never enters the rest
        for f in filenames:
            if regex.match(rel_dir + f):
                p = Path(dirpath) / f
                if p.is_file():
                    matches.append(p)
                    if len(matches) >= max_matches:
                        return matches, f"stopped at the {max_matches}-match cap"
        if clock() > deadline:
            return matches, f"stopped after the {time_budget_s:.0f}s time budget"
    return matches, ""


class Glob:
    name = "glob"
    read_only = True
    description = (
        "Find files by name pattern, e.g. '**/*.py' or 'src/**/test_*.js'. "
        "Returns paths sorted by modification time, newest first. A relative "
        "path is under the project root. Several independent searches can go "
        "in one reply; they run in parallel."
    )
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Directory to search, default cwd"},
        },
        "required": ["pattern"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = Path(args.get("path") or ctx.cwd).expanduser()
        # 2.59.0: a relative path is under the project root (as grep and the
        # hints say), not under whatever the process cwd happens to be.
        if not root.is_absolute():
            root = Path(ctx.cwd) / root
        if not root.is_dir():
            message = f"Not a directory: {root}"
            if args.get("path") and not root.exists():
                message += "\n" + hints.missing_path_hint(args["path"], str(ctx.cwd))
            return ToolResult(message, is_error=True)
        matches, cut = bounded_glob(root, args["pattern"], time_budget_s=GLOB_TIME_BUDGET_S,
                                    max_matches=GLOB_MAX_MATCHES)
        note = (f"\n[partial result: search {cut}; narrow the path or pattern]"
                if cut else "")
        if not matches:
            return ToolResult(f"No files match {args['pattern']!r} under {root}.{note}")

        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0
        matches.sort(key=mtime, reverse=True)
        body = "\n".join(str(p) for p in matches[:400])
        if len(matches) > 400:
            body += f"\n... [{len(matches) - 400} more matches]"
        return ToolResult(body + note)


class Grep:
    name = "grep"
    read_only = True
    description = (
        "Search file contents with a regular expression. Returns matching "
        "lines with file and line number. Use files_only for a file list."
    )
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression"},
            "path": {"type": "string", "description": "File or directory, default cwd"},
            "glob": {"type": "string", "description": "Restrict to matching filenames, e.g. '*.py'"},
            "files_only": {"type": "boolean", "description": "List filenames instead of lines"},
            "ignore_case": {"type": "boolean"},
        },
        "required": ["pattern"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        target = str(Path(args.get("path") or ctx.cwd).expanduser())
        # 2.58.0: a path that is not there gets a pointer at the project
        # root and the nearest match, not rg's bare "No such file" error.
        if args.get("path"):
            check = Path(target)
            if not check.is_absolute():
                check = Path(ctx.cwd) / check
            if not check.exists():
                return ToolResult(f"No such file or directory: {args['path']}\n"
                                  + hints.missing_path_hint(args["path"], str(ctx.cwd)),
                                  is_error=True)
        rg = shutil.which("rg")
        if rg:
            cmd = [rg, "--no-heading", "--line-number", "--color", "never"]
            if args.get("ignore_case"):
                cmd.append("-i")
            if args.get("files_only"):
                cmd.append("--files-with-matches")
            if args.get("glob"):
                cmd += ["--glob", args["glob"]]
            # Exclude the same noise dirs as the grep branch. rg already honours
            # .gitignore, but .venv/build/dist are not always gitignored, so
            # without this the two backends return different results. Placed
            # after any user --glob so the exclusion wins (rg: last glob wins).
            for d in sorted(SKIP_DIRS):
                cmd += ["--glob", f"!**/{d}/**"]
            cmd += ["--", args["pattern"], target]
        else:
            cmd = ["grep", "-rnI", "--color=never"]
            if args.get("ignore_case"):
                cmd.append("-i")
            if args.get("files_only"):
                cmd.append("-l")
            if args.get("glob"):
                cmd += ["--include", args["glob"]]
            for d in SKIP_DIRS:
                cmd += ["--exclude-dir", d]
            cmd += ["-e", args["pattern"], target]

        try:
            # cwd=ctx.cwd: a relative path means the project's, as in run_bash.
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ctx.cwd),
                                  timeout=60, errors="replace")
        except subprocess.TimeoutExpired:
            return ToolResult("Search timed out after 60s. Narrow the pattern or path.",
                              is_error=True)
        # Exit 1 means "no matches" for both rg and grep -- not an error.
        if proc.returncode not in (0, 1):
            return ToolResult(proc.stderr.strip() or f"search failed (exit {proc.returncode})",
                              is_error=True)
        out = proc.stdout.strip()
        if not out:
            return ToolResult(f"No matches for {args['pattern']!r} in {target}.")
        return ToolResult(clamp(out, ctx.config.max_tool_output_chars,
                                ctx.config.max_tool_output_lines))
