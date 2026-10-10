"""Path resolution and change detection in wilbur/tools/files.py."""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.files import _changed_since_read, _resolve


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


def test_resolve_relative_under_ctx_cwd(ctx, tmp_path):
    assert _resolve("a.txt", ctx) == tmp_path / "a.txt"
    assert _resolve("sub/dir/b.txt", ctx) == tmp_path / "sub" / "dir" / "b.txt"


def test_resolve_absolute_stays_absolute(ctx, tmp_path):
    target = tmp_path / "elsewhere" / "a.txt"
    assert _resolve(str(target), ctx) == target


def test_changed_since_read_unread_file_is_not_changed(ctx, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert _changed_since_read(f, ctx) is False


def test_changed_since_read_detects_modification(ctx, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("original")
    ctx.read_files[str(f)] = f.stat().st_mtime
    assert _changed_since_read(f, ctx) is False
    time.sleep(0.01)
    f.write_text("modified")
    assert _changed_since_read(f, ctx) is True


def test_changed_since_read_missing_file_is_not_changed(ctx, tmp_path):
    f = tmp_path / "gone.txt"
    ctx.read_files[str(f)] = 12345.0
    assert _changed_since_read(f, ctx) is False
