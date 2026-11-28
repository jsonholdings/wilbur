"""memory_save / memory_read / memory_forget: the per-project memory store
(wilbur.memory) as tools. Writes go outside the repo, redacted; a save never
changes the current system prompt (it loads next session)."""
from __future__ import annotations

from typing import Any

from .. import memory
from .base import ToolContext, ToolResult


class MemorySave:
    name = "memory_save"
    read_only = True          # writes only Wilbur's own memory dir, never the project
    description = (
        "Remember one durable fact about this project for future sessions (a build/test "
        "command, a convention, a user preference, a decision). Same name updates it in "
        "place. Never store secrets or short-lived details.")
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "short kebab-case name"},
            "type": {"type": "string", "enum": list(memory.TYPES)},
            "description": {"type": "string", "description": "one line for the index"},
            "body": {"type": "string"},
        },
        "required": ["name", "body"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        status, detail = memory.save_fact(
            ctx.cwd, str(args.get("name") or ""), str(args.get("body") or ""),
            type=str(args.get("type") or "project"), description=str(args.get("description") or ""))
        if status == "error":
            return ToolResult(f"memory_save failed: {detail}", is_error=True)
        if status == "duplicate":
            return ToolResult(f"not saved: the same fact is already stored as {detail!r}; "
                              f"save under that name to update it.")
        return ToolResult(f"memory {status}: {detail}")


class MemoryRead:
    name = "memory_read"
    read_only = True
    description = ("Read one remembered fact by name, or list all when name is empty. "
                   "Recalled facts may be stale: verify against the repo before acting.")
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name") or "").strip()
        if not name:
            facts = memory.list_facts(ctx.cwd)
            if not facts:
                return ToolResult("no project memory yet")
            return ToolResult("\n".join(f"- {f['name']} ({f['type']}): {f['description']}"
                                        for f in facts))
        text = memory.read_fact(ctx.cwd, name)
        if text is None:
            return ToolResult(f"no memory named {name!r}", is_error=True)
        return ToolResult("[recalled; may be stale]\n" + text)


class MemoryForget:
    name = "memory_forget"
    read_only = True
    description = "Delete one remembered fact by its exact name (as listed by memory_read)."
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name") or "").strip()
        if memory.forget(ctx.cwd, name):
            return ToolResult(f"forgot {name}")
        return ToolResult(f"no memory named exactly {name!r}", is_error=True)
