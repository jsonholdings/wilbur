"""Tool registry.

Deliberately smaller than Claude Code's. Tool-selection accuracy on local
models degrades noticeably as the schema list grows, so each tool here has to
earn its slot rather than be included for parity.
"""
from __future__ import annotations

from typing import Any

from .base import Approval, Tool, ToolContext, ToolResult, clamp, denial_message
from .files import EditFile, ListFiles, ReadFile, WriteFile
from .search import Glob, Grep
from .shell import Bash
from .skills_tool import ListSkills, LoadSkill
from .test_runner import RunTests
from .todo import TodoWrite


def build_registry(include_task: bool = True, config: Any = None) -> dict[str, Any]:
    tools: list[Any] = [
        Bash(), ReadFile(), WriteFile(), EditFile(),
        Glob(), Grep(), ListFiles(), TodoWrite(),
        RunTests(), ListSkills(), LoadSkill(),
    ]
    # tool-multi-patch: offered only while the switch is on (default on).
    if getattr(config, "apply_patch_tool", True):
        from .patch import ApplyPatch
        tools.insert(4, ApplyPatch())
    # tool-code-intel: off by default until the M-bench shows fewer reads
    # (BACKLOG done-when); turning it on adds one tool to the schema.
    if getattr(config, "code_intel_tool", False):
        from .code_intel import CodeIntel
        tools.append(CodeIntel())
    # tool-git: structured status/diff/log/blame, path-limited commit, gated push.
    if getattr(config, "git_tools", True):
        from .git import git_tools
        tools.extend(git_tools())
    # tool-background-jobs: detached named jobs, stopped at session end.
    if getattr(config, "background_jobs", True):
        from .jobs import job_tools
        tools.extend(job_tools(config))
    if include_task:
        from .task import Task
        tools.append(Task())
    return {t.name: t for t in tools}


def to_schema(registry: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"type": "function",
         "function": {"name": t.name, "description": t.description, "parameters": t.schema}}
        for t in registry.values()
    ]


__all__ = ["Approval", "Tool", "ToolContext", "ToolResult", "clamp", "denial_message",
           "build_registry", "to_schema"]
