"""ListSkills and LoadSkill tool behaviour."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur import config as config_mod
from wilbur.config import Config
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.skills_tool import ListSkills, LoadSkill


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    # Point the user-skill scope at an empty tmp dir so the real
    # ~/.config/wilbur/skills is never read.
    monkeypatch.setattr(config_mod, "USER_SKILLS_DIR", tmp_path / "user_skills")
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


def test_list_skills_lists_builtins(ctx):
    out = ListSkills().run({}, ctx)
    assert not out.is_error
    assert "bench-tasks" in out.output
    assert "writing-tests" in out.output


def test_load_skill_returns_body(ctx):
    out = LoadSkill().run({"name": "writing-tests"}, ctx)
    assert not out.is_error
    assert "pytest" in out.output


def test_load_skill_unknown_name_is_error(ctx):
    out = LoadSkill().run({"name": "no-such-skill"}, ctx)
    assert out.is_error
    assert "no-such-skill" in out.output
    assert "Available" in out.output
