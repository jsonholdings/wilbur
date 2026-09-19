"""Glob and grep, shelling out to fd/rg when present and falling back cleanly."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from .base import ToolContext, ToolResult, clamp

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             "dist", "build", ".mypy_cache", ".pytest_cache"}


class Glob:
    name = "glob"
    read_only = True
    description = (
        "Find files by name pattern, e.g. '**/*.py' or 'src/**/test_*.js'. "
        "Returns paths sorted by modification time, newest first."
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
        if not root.is_dir():
            return ToolResult(f"Not a directory: {root}", is_error=True)
        matches = []
        for p in root.glob(args["pattern"]):
            if not p.is_file():
                continue
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            matches.append(p)
        if not matches:
            return ToolResult(f"No files match {args['pattern']!r} under {root}.")
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        body = "\n".join(str(p) for p in matches[:400])
        if len(matches) > 400:
            body += f"\n... [{len(matches) - 400} more matches]"
        return ToolResult(body)


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
            proc = subprocess.run(cmd, capture_output=True, text=True,
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
