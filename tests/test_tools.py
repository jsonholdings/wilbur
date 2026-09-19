"""Filesystem, search and clamping behaviour."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools import build_registry
from wilbur.tools.base import Approval, ToolContext, clamp


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


@pytest.fixture
def tools():
    return build_registry()


def test_read_numbers_lines(tmp_path, ctx, tools):
    f = tmp_path / "a.txt"
    f.write_text("alpha\nbeta\n")
    out = tools["read_file"].run({"path": str(f)}, ctx)
    assert not out.is_error
    assert "1\talpha" in out.output and "2\tbeta" in out.output


def test_read_marks_file_as_read(tmp_path, ctx, tools):
    f = tmp_path / "a.txt"
    f.write_text("x")
    tools["read_file"].run({"path": str(f)}, ctx)
    assert str(f) in ctx.read_files


def test_write_refuses_unread_overwrite(tmp_path, ctx, tools):
    f = tmp_path / "a.txt"
    f.write_text("original")
    out = tools["write_file"].run({"path": str(f), "content": "new"}, ctx)
    assert out.is_error
    assert f.read_text() == "original"


def test_write_creates_new_file_without_reading(tmp_path, ctx, tools):
    f = tmp_path / "sub" / "new.txt"
    out = tools["write_file"].run({"path": str(f), "content": "hello"}, ctx)
    assert not out.is_error
    assert f.read_text() == "hello"


def test_edit_requires_prior_read(tmp_path, ctx, tools):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    out = tools["edit_file"].run(
        {"path": str(f), "old_string": "x = 1", "new_string": "x = 2"}, ctx)
    assert out.is_error and "Read" in out.output


def test_edit_replaces_once(tmp_path, ctx, tools):
    f = tmp_path / "a.py"
    f.write_text("x = 1\ny = 2\n")
    tools["read_file"].run({"path": str(f)}, ctx)
    out = tools["edit_file"].run(
        {"path": str(f), "old_string": "x = 1", "new_string": "x = 99"}, ctx)
    assert not out.is_error
    assert f.read_text() == "x = 99\ny = 2\n"


def test_edit_rejects_ambiguous_match(tmp_path, ctx, tools):
    f = tmp_path / "a.py"
    f.write_text("v = 0\nv = 0\n")
    tools["read_file"].run({"path": str(f)}, ctx)
    out = tools["edit_file"].run(
        {"path": str(f), "old_string": "v = 0", "new_string": "v = 1"}, ctx)
    assert out.is_error and "2 times" in out.output


def test_edit_replace_all(tmp_path, ctx, tools):
    f = tmp_path / "a.py"
    f.write_text("v = 0\nv = 0\n")
    tools["read_file"].run({"path": str(f)}, ctx)
    out = tools["edit_file"].run(
        {"path": str(f), "old_string": "v = 0", "new_string": "v = 1",
         "replace_all": True}, ctx)
    assert not out.is_error
    assert f.read_text() == "v = 1\nv = 1\n"


def test_edit_indentation_hint(tmp_path, ctx, tools):
    """A bare 'not found' makes a local model retry blindly; name the cause.

    The real failure: the model reproduces the right lines but flattens the
    leading whitespace, so the exact match misses.
    """
    f = tmp_path / "a.py"
    f.write_text("def f():\n    return 1\n")
    tools["read_file"].run({"path": str(f)}, ctx)
    out = tools["edit_file"].run(
        {"path": str(f), "old_string": "def f():\nreturn 1",
         "new_string": "def f():\nreturn 2"}, ctx)
    assert out.is_error
    assert "indentation" in out.output.lower()
    assert f.read_text() == "def f():\n    return 1\n"


def test_edit_later_line_mismatch_hint(tmp_path, ctx, tools):
    f = tmp_path / "a.py"
    f.write_text("def f():\n    return 1\n")
    tools["read_file"].run({"path": str(f)}, ctx)
    out = tools["edit_file"].run(
        {"path": str(f), "old_string": "def f():\n    return 42",
         "new_string": "x"}, ctx)
    assert out.is_error
    assert "further down" in out.output


def test_edit_substring_match_still_works(tmp_path, ctx, tools):
    """An indented line is matchable by its stripped content -- that is fine."""
    f = tmp_path / "a.py"
    f.write_text("def f():\n    return 1\n")
    tools["read_file"].run({"path": str(f)}, ctx)
    out = tools["edit_file"].run(
        {"path": str(f), "old_string": "return 1", "new_string": "return 2"}, ctx)
    assert not out.is_error
    assert f.read_text() == "def f():\n    return 2\n"


def test_bash_denylist(tmp_path, ctx, tools):
    out = tools["run_bash"].run({"command": "rm -rf / --no-preserve-root"}, ctx)
    assert out.is_error and "deny-list" in out.output


def test_bash_reports_nonzero_exit(ctx, tools):
    out = tools["run_bash"].run({"command": "exit 3"}, ctx)
    assert out.is_error and "exit 3" in out.output


def test_bash_denied_by_user(tmp_path, tools):
    ctx = ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                      approve=lambda tool, detail: Approval.DENIED)
    out = tools["run_bash"].run({"command": "echo hi"}, ctx)
    assert out.is_error and "denied" in out.output


def test_grep_finds_match(tmp_path, ctx, tools):
    (tmp_path / "a.py").write_text("def target():\n    pass\n")
    out = tools["grep"].run({"pattern": "def target", "path": str(tmp_path)}, ctx)
    assert not out.is_error and "target" in out.output


def test_grep_no_match_is_not_an_error(tmp_path, ctx, tools):
    (tmp_path / "a.py").write_text("nothing\n")
    out = tools["grep"].run({"pattern": "zzz_absent", "path": str(tmp_path)}, ctx)
    assert not out.is_error and "No matches" in out.output


def test_glob_skips_vendor_dirs(tmp_path, ctx, tools):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("")
    (tmp_path / "real.py").write_text("")
    out = tools["glob"].run({"pattern": "**/*.py", "path": str(tmp_path)}, ctx)
    assert "real.py" in out.output
    assert "node_modules" not in out.output


def test_clamp_keeps_head_and_tail():
    text = "\n".join(str(i) for i in range(5000))
    out = clamp(text, 100000, 100)
    assert "0" in out.splitlines()[0]
    assert "4999" in out.splitlines()[-1]
    assert "omitted" in out


def test_todo_renders_and_counts(ctx, tools):
    out = tools["todo_write"].run({"todos": [
        {"task": "one", "status": "completed"},
        {"task": "two", "status": "in_progress"},
    ]}, ctx)
    assert "[x] one" in out.output and "[>] two" in out.output
    assert "1/2 complete" in out.output
