"""Bash execution with a deny-list and hard output clamping."""
from __future__ import annotations

import subprocess
from typing import Any

from .base import Approval, ToolContext, ToolResult, clamp, denial_message


class Bash:
    name = "run_bash"
    read_only = False
    description = (
        "Run a shell command and return its combined stdout and stderr. "
        "Use for builds, tests, git, and any command-line tool. Prefer the "
        "dedicated search tools over find and grep."
    )
    schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run"},
            "timeout": {"type": "integer", "description": "Seconds, default 120"},
            "description": {"type": "string",
                            "description": "5-10 word description of what this does"},
        },
        "required": ["command"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args.get("command", "").strip()
        if not command:
            return ToolResult("No command given.", is_error=True)

        lowered = command.lower()
        for pattern in ctx.config.denied_bash:
            if pattern.lower() in lowered:
                return ToolResult(
                    f"Refused: command matches the deny-list entry {pattern!r}.",
                    is_error=True,
                )

        label = args.get("description") or command[:70]
        outcome = ctx.approve("run_bash", f"{label}\n    $ {command}")
        if outcome is not Approval.GRANTED:
            return ToolResult(denial_message("command", outcome), is_error=True)

        timeout = int(args.get("timeout", 120))
        try:
            proc = subprocess.run(
                command, shell=True, cwd=ctx.cwd, capture_output=True,
                text=True, timeout=timeout, errors="replace",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(f"Command timed out after {timeout}s.", is_error=True)

        body = (proc.stdout or "") + (proc.stderr or "")
        body = clamp(body.strip(), ctx.config.max_tool_output_chars,
                     ctx.config.max_tool_output_lines)
        if proc.returncode != 0:
            return ToolResult(f"exit {proc.returncode}\n{body}" if body
                              else f"exit {proc.returncode} (no output)", is_error=True)
        return ToolResult(body or "(no output)")
