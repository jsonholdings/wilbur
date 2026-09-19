"""Tools for listing and loading SKILL.md-based skills.

Named skills_tool.py, not skills.py, to avoid shadowing wilbur/skills.py
(the discovery module these tools wrap) on sys.path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import config as config_mod
from ..skills import discover_skills, load_skill_body
from .base import ToolContext, ToolResult, clamp


class ListSkills:
    name = "list_skills"
    read_only = True
    description = (
        "List available skills (name and one-line description). A skill's "
        "body is not loaded until load_skill is called with its name."
    )
    schema = {"type": "object", "properties": {}}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        skills = discover_skills(Path(ctx.cwd), config_mod.USER_SKILLS_DIR)
        if not skills:
            return ToolResult("No skills available.")
        body = "\n".join(f"{s.name}: {s.description}" for s in skills)
        return ToolResult(body)


class LoadSkill:
    name = "load_skill"
    read_only = True
    description = "Load the full body of a skill by name (see list_skills)."
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name from list_skills"},
        },
        "required": ["name"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        wanted = args.get("name", "")
        skills = discover_skills(Path(ctx.cwd), config_mod.USER_SKILLS_DIR)
        by_name = {s.name: s for s in skills}
        skill = by_name.get(wanted)
        if skill is None:
            available = ", ".join(sorted(by_name)) or "(none)"
            return ToolResult(
                f"No skill named {wanted!r}. Available: {available}", is_error=True
            )
        body = load_skill_body(skill)
        return ToolResult(clamp(body, ctx.config.max_tool_output_chars,
                                 ctx.config.max_tool_output_lines))
