"""Glob tool: pattern matching on a tmp_path tree, no-match behaviour."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wilbur.config import Config
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.search import Glob


def _ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


def test_starstar_matches_nested_files(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "deep").mkdir()
    (tmp_path / "src" / "deep" / "b.py").write_text("y = 2\n")
    (tmp_path / "src" / "deep" / "c.txt").write_text("nope\n")
    out = Glob().run({"pattern": "**/*.py", "path": str(tmp_path)}, _ctx(tmp_path))
    assert not out.is_error
    assert str(tmp_path / "a.py") in out.output
    assert str(tmp_path / "src" / "deep" / "b.py") in out.output
    assert "c.txt" not in out.output


def test_no_match_returns_result_without_raising(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    out = Glob().run({"pattern": "**/*.zig", "path": str(tmp_path)}, _ctx(tmp_path))
    assert not out.is_error
    assert "No files match" in out.output
    assert "*.zig" in out.output


def test_non_directory_path_is_an_error(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    out = Glob().run({"pattern": "**/*.py", "path": str(f)}, _ctx(tmp_path))
    assert out.is_error
    assert "Not a directory" in out.output


def test_skip_dirs_are_excluded(tmp_path):
    (tmp_path / "top.py").write_text("x = 1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.py").write_text("y = 2\n")
    out = Glob().run({"pattern": "**/*.py", "path": str(tmp_path)}, _ctx(tmp_path))
    assert not out.is_error
    assert str(tmp_path / "top.py") in out.output
    assert "pkg.py" not in out.output
