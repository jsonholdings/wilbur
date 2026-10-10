"""ListFiles tool: directory listing, ordering, and error handling."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools import build_registry
from wilbur.tools.base import Approval, ToolContext


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


@pytest.fixture
def tools():
    return build_registry()


def test_lists_files_and_subdirectories(ctx, tools, tmp_path):
    (tmp_path / "alpha.txt").write_text("a")
    (tmp_path / "beta.py").write_text("b")
    (tmp_path / "subdir").mkdir()
    out = tools["list_files"].run({"path": str(tmp_path)}, ctx)
    assert not out.is_error
    assert "alpha.txt" in out.output
    assert "beta.py" in out.output
    # Subdirectories are listed with a trailing slash.
    assert "subdir/" in out.output


def test_missing_directory_is_an_error_not_a_raise(ctx, tools, tmp_path):
    out = tools["list_files"].run({"path": str(tmp_path / "nope")}, ctx)
    assert out.is_error
    assert "Not a directory" in out.output


def test_path_is_a_file_is_an_error(ctx, tools, tmp_path):
    f = tmp_path / "plain.txt"
    f.write_text("x")
    out = tools["list_files"].run({"path": str(f)}, ctx)
    assert out.is_error
    assert "Not a directory" in out.output


def test_relative_path_resolves_against_cwd(ctx, tools, tmp_path):
    (tmp_path / "rel.txt").write_text("r")
    out = tools["list_files"].run({"path": "."}, ctx)
    assert not out.is_error
    assert "rel.txt" in out.output


def test_empty_directory_reports_empty(ctx, tools, tmp_path):
    out = tools["list_files"].run({"path": str(tmp_path)}, ctx)
    assert not out.is_error
    assert "(empty)" in out.output


def test_dotfiles_are_hidden_except_env_example(ctx, tools, tmp_path):
    (tmp_path / ".secret").write_text("s")
    (tmp_path / ".env.example").write_text("e")
    (tmp_path / "visible.txt").write_text("v")
    out = tools["list_files"].run({"path": str(tmp_path)}, ctx)
    assert not out.is_error
    assert ".secret" not in out.output
    assert ".env.example" in out.output
    assert "visible.txt" in out.output
