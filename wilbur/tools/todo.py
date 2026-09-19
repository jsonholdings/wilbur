"""Task list the agent maintains across a multi-step job.

Local models lose the thread on long tasks far more readily than a frontier
model does. An explicit, re-stated plan in context is the cheapest correction
available.
"""
from __future__ import annotations

from typing import Any

from .base import ToolContext, ToolResult

STATES = ("pending", "in_progress", "completed")
MARKS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}


class TodoWrite:
    name = "todo_write"
    read_only = True          # touches no user state, only the session's plan
    description = (
        "Record or update the task list for the current job. Pass the complete "
        "list every time. Mark exactly one task in_progress while you work on "
        "it, and mark it completed before starting the next."
    )
    schema = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string"},
                        "status": {"type": "string", "enum": list(STATES)},
                    },
                    "required": ["task", "status"],
                },
            }
        },
        "required": ["todos"],
    }

    def __init__(self) -> None:
        self.todos: list[dict[str, str]] = []

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        todos = args.get("todos") or []
        cleaned = []
        for item in todos:
            if not isinstance(item, dict) or "task" not in item:
                continue
            status = item.get("status", "pending")
            cleaned.append({"task": str(item["task"]),
                            "status": status if status in STATES else "pending"})
        if not cleaned:
            return ToolResult("No valid todos supplied.", is_error=True)

        active = [t for t in cleaned if t["status"] == "in_progress"]
        self.todos = cleaned
        # The plan belongs to the harness, not the transcript. Held here it
        # survives compaction and is re-rendered into the briefing every turn;
        # left as a tool result it gets summarised away exactly when a long job
        # needs it most.
        if getattr(ctx, "state", None) is not None:
            ctx.state.set_plan(cleaned)
        rendered = "\n".join(f"{MARKS[t['status']]} {t['task']}" for t in cleaned)
        warning = ""
        if len(active) > 1:
            warning = "\n(Note: more than one task is in_progress. Work on one at a time.)"
        done = sum(1 for t in cleaned if t["status"] == "completed")
        return ToolResult(f"{rendered}\n\n{done}/{len(cleaned)} complete.{warning}",
                          display=rendered)
