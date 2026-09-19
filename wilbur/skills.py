"""Discovery for SKILL.md-based skills.

A skill is a directory containing a `SKILL.md` whose top is a small
frontmatter block:

    ---
    name: skill-name
    description: one line the model uses to decide whether to load it
    ---
    (free-form body, loaded only on demand)

Skills come from three scopes, closest wins on a name collision:
project (`<project_dir>/.wilbur/skills/*/SKILL.md`), then user
(`<user_dir>/*/SKILL.md`), then builtin (shipped with this package). The
frontmatter is two flat keys, so it is parsed with a manual split on the
`---` delimiters rather than pulling in a yaml dependency.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    name: str
    description: str
    path: Path


def _parse_skill_md(path: Path) -> Skill | None:
    """Return a Skill for a well-formed SKILL.md, else None (never raises).

    A malformed file -- no frontmatter, or missing name/description -- is
    silently skipped so one bad skill doesn't break discovery of the rest.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    frontmatter = parts[1]
    name = ""
    description = ""
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith("name:"):
            name = line[len("name:"):].strip()
        elif line.startswith("description:"):
            description = line[len("description:"):].strip()
    if not name or not description:
        return None
    return Skill(name=name, description=description, path=path)


def _scan(root: Path) -> list[Skill]:
    if not root.is_dir():
        return []
    found = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        skill = _parse_skill_md(skill_md)
        if skill is not None:
            found.append(skill)
    return found


def discover_skills(project_dir: Path, user_dir: Path) -> list[Skill]:
    """Discover skills from project, user, and builtin scopes.

    Ordering on a name collision: project wins, then user, then builtin --
    the closest-scope skill shadows a same-named one further out.
    """
    builtin_dir = Path(__file__).parent / "builtin_skills"
    by_name: dict[str, Skill] = {}
    # Insert lowest-priority first so a later scope's entry overwrites it.
    for skill in _scan(builtin_dir):
        by_name[skill.name] = skill
    for skill in _scan(user_dir):
        by_name[skill.name] = skill
    for skill in _scan(project_dir / ".wilbur" / "skills"):
        by_name[skill.name] = skill
    return sorted(by_name.values(), key=lambda s: s.name)


def load_skill_body(skill: Skill) -> str:
    """Return the free-form body text after the closing `---`."""
    text = skill.path.read_text(encoding="utf-8")
    return text.split("---", 2)[2].lstrip("\n")
