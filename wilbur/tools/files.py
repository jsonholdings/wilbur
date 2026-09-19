"""Filesystem tools: read, write, edit, list."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .base import Approval, ToolContext, ToolResult, clamp, denial_message

TEXT_SUFFIXES_BINARY = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz",
                        ".tar", ".so", ".bin", ".exe", ".woff", ".woff2", ".ico"}


def _resolve(path: str, ctx: ToolContext) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(ctx.cwd) / p
    return p


def _changed_since_read(path: Path, ctx: ToolContext) -> bool:
    """True if the file's mtime differs from when this session last read it.

    read/write/edit all record st_mtime in ctx.read_files; without comparing it
    a write would silently clobber a change made on disk after the read (a lost
    update). This makes that recorded mtime actually mean something.
    """
    known = ctx.read_files.get(str(path))
    if known is None:
        return False
    try:
        return path.stat().st_mtime != known
    except OSError:
        return False


class ReadFile:
    name = "read_file"
    read_only = True
    description = (
        "Read a file from disk. Returns the contents with 1-indexed line numbers. "
        "Use before editing any file. For large files pass offset and limit."
    )
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or relative file path"},
            "offset": {"type": "integer", "description": "1-indexed first line to read"},
            "limit": {"type": "integer", "description": "How many lines to read"},
        },
        "required": ["path"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(args["path"], ctx)
        if not path.exists():
            return ToolResult(f"File does not exist: {path}", is_error=True)
        if path.is_dir():
            return ToolResult(f"{path} is a directory. Use list_files.", is_error=True)
        if path.suffix.lower() in TEXT_SUFFIXES_BINARY:
            return ToolResult(f"{path} is a binary file ({path.stat().st_size} bytes).",
                              is_error=True)
        try:
            raw = path.read_text(errors="replace")
        except OSError as exc:
            return ToolResult(f"Could not read {path}: {exc}", is_error=True)

        ctx.read_files[str(path)] = path.stat().st_mtime

        lines = raw.splitlines()
        offset = max(1, int(args.get("offset", 1)))
        limit = int(args.get("limit", ctx.config.max_read_lines))
        chunk = lines[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{offset + i:6d}\t{line}" for i, line in enumerate(chunk))
        note = ""
        if offset - 1 + len(chunk) < len(lines):
            note = (f"\n\n[showing lines {offset}-{offset + len(chunk) - 1} "
                    f"of {len(lines)}; pass offset to continue]")
        if not chunk:
            return ToolResult(f"[{path} is empty]" if not lines
                              else f"[offset {offset} is past end of file ({len(lines)} lines)]")
        return ToolResult(clamp(numbered, ctx.config.max_tool_output_chars,
                                ctx.config.max_tool_output_lines) + note)


class WriteFile:
    name = "write_file"
    read_only = False
    description = (
        "Write content to a file, creating it or replacing it entirely. "
        "To change part of an existing file use edit_file instead."
    )
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(args["path"], ctx)
        content = args.get("content", "")
        existed = path.exists()
        if existed and str(path) not in ctx.read_files:
            return ToolResult(
                f"Refusing to overwrite {path} without reading it first. "
                "Call read_file on it, then retry.",
                is_error=True,
            )
        if existed and _changed_since_read(path, ctx):
            return ToolResult(
                f"{path} changed on disk since you read it -- overwriting would "
                "lose that change. Re-read it, then retry.",
                is_error=True,
            )
        outcome = ctx.approve("write_file", f"write {len(content)} bytes to {path}")
        if outcome is not Approval.GRANTED:
            return ToolResult(denial_message("write", outcome), is_error=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        ctx.read_files[str(path)] = path.stat().st_mtime
        verb = "Updated" if existed else "Created"
        return ToolResult(f"{verb} {path} ({len(content.splitlines())} lines).")


class EditFile:
    name = "edit_file"
    read_only = False
    description = (
        "Replace an exact string in a file. old_string must match the file "
        "exactly, including indentation, and must be unique unless replace_all "
        "is true. Read the file first."
    )
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "Exact text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(args["path"], ctx)
        if not path.exists():
            return ToolResult(f"File does not exist: {path}", is_error=True)
        if str(path) not in ctx.read_files:
            return ToolResult(
                f"Read {path} before editing it.", is_error=True)
        if _changed_since_read(path, ctx):
            return ToolResult(
                f"{path} changed on disk since you read it. Re-read it, then retry.",
                is_error=True)

        old = args["old_string"]
        new = args["new_string"]
        if old == new:
            return ToolResult("old_string and new_string are identical.", is_error=True)

        text = path.read_text(errors="replace")
        count = text.count(old)

        if count == 0:
            hint = _near_miss_hint(text, old)
            return ToolResult(
                f"old_string not found in {path}.{hint}", is_error=True)
        if count > 1 and not args.get("replace_all"):
            return ToolResult(
                f"old_string appears {count} times in {path}. Add more surrounding "
                "context to make it unique, or pass replace_all.",
                is_error=True,
            )
        outcome = ctx.approve("edit_file", f"edit {path}")
        if outcome is not Approval.GRANTED:
            return ToolResult(denial_message("edit", outcome), is_error=True)

        updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        path.write_text(updated)
        ctx.read_files[str(path)] = path.stat().st_mtime
        return ToolResult(f"Edited {path} ({count if args.get('replace_all') else 1} "
                          f"replacement{'s' if args.get('replace_all') and count > 1 else ''}).")


def _near_miss_hint(text: str, old: str) -> str:
    """Tell the model *why* an exact match failed.

    Local models retry blindly on a bare 'not found'. Naming the actual
    difference -- almost always whitespace -- gets a correct second attempt
    instead of three more wrong ones.
    """
    stripped = "\n".join(line.strip() for line in old.splitlines())
    haystack = "\n".join(line.strip() for line in text.splitlines())
    if stripped and stripped in haystack:
        return (" The text is present but the indentation differs. "
                "Re-read the file and copy the leading whitespace exactly.")
    first = old.splitlines()[0].strip() if old.splitlines() else ""
    if first and first in text:
        return (f" The first line ({first[:60]!r}) is present, so the mismatch is "
                "further down. Re-read that region and copy it verbatim.")
    return " No similar text found either -- re-read the file."


class ListFiles:
    name = "list_files"
    read_only = True
    description = "List the entries of a directory."
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(args.get("path", "."), ctx)
        if not path.is_dir():
            return ToolResult(f"Not a directory: {path}", is_error=True)
        entries = []
        for item in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if item.name.startswith(".") and item.name not in {".env.example"}:
                continue
            entries.append(f"{item.name}/" if item.is_dir() else item.name)
        body = "\n".join(entries) or "(empty)"
        return ToolResult(clamp(body, ctx.config.max_tool_output_chars,
                                ctx.config.max_tool_output_lines))
