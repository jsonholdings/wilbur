"""Skill discovery, loading and the list_skills/load_skill tools."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import wilbur.config as config_mod
from wilbur.config import Config
from wilbur.skills import discover_skills, load_skill_body
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.skills_tool import ListSkills, LoadSkill


def _write_skill(dir_path: Path, name: str, description: str, body: str = "body text") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    skill_md = dir_path / "SKILL.md"
    skill_md.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n")
    return skill_md


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                        approve=lambda tool, detail: Approval.GRANTED)


def test_discovers_project_skill(tmp_path):
    _write_skill(tmp_path / ".wilbur" / "skills" / "foo", "foo", "does foo things")
    skills = discover_skills(tmp_path, tmp_path / "nonexistent-user-dir")
    names = {s.name: s.description for s in skills}
    assert names["foo"] == "does foo things"


def test_discovers_user_skill(tmp_path, monkeypatch):
    user_dir = tmp_path / "user-skills"
    _write_skill(user_dir / "bar", "bar", "does bar things")
    monkeypatch.setattr(config_mod, "USER_SKILLS_DIR", user_dir)
    skills = discover_skills(tmp_path / "empty-project", user_dir)
    names = {s.name: s.description for s in skills}
    assert names["bar"] == "does bar things"


def test_malformed_skill_skipped_but_sibling_found(tmp_path):
    skills_root = tmp_path / ".wilbur" / "skills"
    # No frontmatter at all.
    bad_dir = skills_root / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text("just some text, no frontmatter\n")
    # Frontmatter present but missing description:.
    bad_dir2 = skills_root / "bad2"
    bad_dir2.mkdir(parents=True)
    (bad_dir2 / "SKILL.md").write_text("---\nname: bad2\n---\nbody\n")
    # Control: a well-formed sibling in the same scan.
    _write_skill(skills_root / "good", "good", "a fine skill")

    skills = discover_skills(tmp_path, tmp_path / "no-user-dir")
    names = {s.name for s in skills}
    assert "bad" not in names
    assert "bad2" not in names
    assert "good" in names  # control: proves the skip isn't "found nothing" overall


def test_load_skill_body_returns_exact_body(tmp_path):
    skill_dir = tmp_path / ".wilbur" / "skills" / "foo"
    _write_skill(skill_dir, "foo", "does foo things", body="line one\nline two")
    skills = discover_skills(tmp_path, tmp_path / "no-user-dir")
    skill = next(s for s in skills if s.name == "foo")
    assert load_skill_body(skill) == "line one\nline two\n"


def test_project_skill_overrides_user_and_builtin(tmp_path, monkeypatch):
    user_dir = tmp_path / "user-skills"
    _write_skill(user_dir / "writing-tests", "writing-tests", "user override description")
    monkeypatch.setattr(config_mod, "USER_SKILLS_DIR", user_dir)
    _write_skill(tmp_path / ".wilbur" / "skills" / "writing-tests", "writing-tests",
                 "project override description")
    skills = discover_skills(tmp_path, user_dir)
    match = next(s for s in skills if s.name == "writing-tests")
    assert match.description == "project override description"


def test_user_overrides_builtin(tmp_path, monkeypatch):
    user_dir = tmp_path / "user-skills"
    _write_skill(user_dir / "writing-tests", "writing-tests", "user override description")
    monkeypatch.setattr(config_mod, "USER_SKILLS_DIR", user_dir)
    skills = discover_skills(tmp_path / "empty-project", user_dir)
    match = next(s for s in skills if s.name == "writing-tests")
    assert match.description == "user override description"


def test_builtin_skills_discoverable_with_no_project_or_user_dir(tmp_path):
    skills = discover_skills(tmp_path / "empty-project", tmp_path / "empty-user")
    names = {s.name for s in skills}
    assert {"writing-tests", "bench-tasks", "changelog-entries"} <= names


def test_list_skills_tool(tmp_path, ctx):
    out = ListSkills().run({}, ctx)
    assert not out.is_error
    assert "writing-tests:" in out.output


def test_list_skills_tool_empty(tmp_path, ctx, monkeypatch):
    monkeypatch.setattr("wilbur.tools.skills_tool.discover_skills", lambda *a, **k: [])
    out = ListSkills().run({}, ctx)
    assert out.output == "No skills available."


def test_load_skill_tool_returns_body(tmp_path, ctx):
    out = LoadSkill().run({"name": "writing-tests"}, ctx)
    assert not out.is_error
    assert "isolate_wilbur_home" in out.output


def test_load_skill_tool_unknown_name_lists_available(tmp_path, ctx):
    out = LoadSkill().run({"name": "does-not-exist"}, ctx)
    assert out.is_error
    assert "does-not-exist" in out.output
    assert "writing-tests" in out.output
