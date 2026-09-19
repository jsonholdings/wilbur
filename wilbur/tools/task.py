"""Subagent tool.

A subagent runs its own agent loop in a fresh context and returns only its
final report. On a 64K window that isolation is not a convenience -- a search
that reads thirty files would otherwise evict the main task from context. The
subagent absorbs that cost and hands back a paragraph.

Subagents cannot spawn subagents; nesting multiplies context and wall time for
no measured benefit on local models.
"""
from __future__ import annotations

import threading
from typing import Any

from . import agents_registry
from .base import Approval, ToolContext, ToolResult, clamp, denial_message

AGENT_TYPES: dict[str, dict[str, Any]] = {
    "explore": {
        "tools": ["read_file", "glob", "grep", "list_files", "run_bash"],
        "read_only": True,
        "prompt": (
            "You are a search subagent. Locate what was asked for and report "
            "back concisely: exact file paths, line numbers, and the few lines "
            "that matter. Quote sparingly. Do not modify anything. Your caller "
            "sees only your final message, so it must stand alone."
        ),
    },
    "general": {
        "tools": None,          # everything except task itself
        "read_only": False,
        "prompt": (
            "You are a subagent handling a delegated task end to end. Your "
            "caller sees only your final message, so state what you did, what "
            "the outcome was, and anything that still needs attention."
        ),
    },
}


class Task:
    name = "task"
    read_only = False
    description = (
        "Delegate a self-contained job to a subagent with its own context. Use "
        "for broad searches across many files, or for a chunk of work whose "
        "intermediate steps you do not need to see. The subagent returns only "
        "its final report. Give it a complete, standalone instruction -- it "
        "cannot see this conversation."
    )
    schema = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Full standalone instruction for the subagent",
            },
            "agent_type": {
                "type": "string",
                "enum": sorted(AGENT_TYPES),
                "description": "'explore' for read-only search, 'general' for work",
            },
            "description": {
                "type": "string",
                "description": "3-5 word label for this task",
            },
        },
        "required": ["prompt"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..agent import Agent      # imported here to break the cycle

        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult("task requires a prompt.", is_error=True)

        kind = args.get("agent_type") or "explore"
        spec = AGENT_TYPES.get(kind)
        if spec is None:
            return ToolResult(
                f"Unknown agent_type {kind!r}. Choose one of: "
                f"{', '.join(sorted(AGENT_TYPES))}.",
                is_error=True,
            )

        label = args.get("description") or kind
        if not spec["read_only"]:
            outcome = ctx.approve("task", f"run '{label}' subagent")
            if outcome is not Approval.GRANTED:
                return ToolResult(denial_message("subagent", outcome), is_error=True)

        # Give the subagent the standing goal as *context*, not as its task.
        # Without it a delegated worker optimises for its slice in isolation --
        # renaming a symbol its caller needed, or "fixing" a test it was meant
        # to make pass. With it, the slice is still the job but the reason for
        # the job is visible. Its own objective stays the prompt it was handed.
        extra = spec["prompt"]
        parent = getattr(ctx, "state", None)
        if parent is not None and parent.objective:
            extra += ("\n\nContext -- the larger job this is part of. Do NOT take this "
                      "on yourself; it is here so your slice fits it:\n"
                      + parent.objective.strip()[:800])
            cur = parent.current
            if cur:
                extra += f"\n\nThe caller's current plan item: {cur.text}"

        from dataclasses import replace as _dc_replace
        from ..state import RunState
        # A copy, not the parent's own Config object: the subagent gets its
        # own round budget (`subagent_max_turns`) instead of inheriting
        # whatever `max_turns` the parent happens to be set to, and mutating
        # it here must never leak back and change the parent's setting.
        sub_config = _dc_replace(ctx.config, max_turns=ctx.config.subagent_max_turns)

        # Registered before the thread starts so `/agents` and the spinner
        # can see it the moment it exists, not only once it finishes.
        record = agents_registry.register(label, kind)
        sub_cancel = threading.Event()

        def _sub_event(kind_: str, data: dict) -> None:
            if kind_ == "assistant":
                agents_registry.update(record.id, round=record.round + 1)
            elif kind_ == "tool_start":
                agents_registry.update(record.id, current=f"tool: {data.get('name', '')}")
            elif kind_ == "tool_end":
                agents_registry.update(record.id, current="")

        sub = Agent(
            config=sub_config,
            cwd=ctx.cwd,
            approve=ctx.approve,
            on_event=_sub_event,
            model=getattr(ctx.config, "subagent_model", None) or ctx.config.model,
            tool_names=spec["tools"],
            include_task=False,       # no nesting
            system_extra=extra,
            state=RunState(),         # its own plan and ledger, not the caller's
            cancel=sub_cancel,
        )
        # sub.run() is synchronous and executes inside THIS tool call, so
        # nothing else on the call stack bounds how long it runs -- a stuck
        # subagent (a slow/CPU-spilled model looping through max_turns) hangs
        # the whole CLI with no feedback, which is exactly the "never
        # returns" failure this timeout exists to prevent. It runs on a
        # daemon thread so the timeout can actually walk away from it: Python
        # has no safe way to kill a running thread, so a timed-out subagent
        # keeps consuming its slot in the background (visible as an orphaned
        # thread) rather than the whole process being unkillable.
        outcome_box: dict[str, Any] = {}

        def _run() -> None:
            try:
                outcome_box["report"] = sub.run(prompt)
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller
                outcome_box["exc"] = exc

        timeout_s = getattr(ctx.config, "subagent_timeout_s", 300)
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        try:
            thread.join(timeout_s)
        except KeyboardInterrupt:
            # Ctrl-C on the main thread lands here (this call is still on the
            # caller's own thread, blocked in join()), not on the subagent's
            # daemon thread -- setting the flag is what actually reaches it.
            # sub.run() checks it between rounds and before each tool call,
            # so give it a short grace period to notice and unwind cleanly
            # before this tool call itself gives up and re-raises.
            sub_cancel.set()
            thread.join(5.0)
            agents_registry.finish(record.id, "cancelled")
            raise

        if thread.is_alive():
            agents_registry.finish(record.id, "timeout")
            return ToolResult(
                f"Subagent '{label}' timed out after {timeout_s}s and was "
                "abandoned. It may still be running in the background; its "
                "work was not applied to this conversation. Try a narrower "
                "prompt, a smaller/faster model, or raise subagent_timeout_s.",
                is_error=True,
            )

        if "exc" in outcome_box:
            exc = outcome_box["exc"]
            agents_registry.finish(record.id, "error")
            return ToolResult(f"Subagent failed: {type(exc).__name__}: {exc}",
                              is_error=True)

        report = outcome_box.get("report", "")
        if not report.strip():
            agents_registry.finish(record.id, "error")
            return ToolResult("Subagent returned no report.", is_error=True)
        agents_registry.finish(record.id, "done")

        # A read-only subagent must not be able to launder a write through its
        # report; surface what it touched so the caller can verify.
        touched = sorted(sub.ctx.read_files)
        footer = ""
        if touched and len(touched) <= 12:
            footer = "\n\n[subagent read: " + ", ".join(touched) + "]"

        return ToolResult(
            clamp(report + footer, ctx.config.max_tool_output_chars,
                  ctx.config.max_tool_output_lines),
            display=f"subagent '{label}' returned {len(report)} chars",
        )
