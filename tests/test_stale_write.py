"""The 'file changed on disk since you read it' guard.

read/write/edit record st_mtime; these check it is actually enforced, so a
concurrent change on disk is not silently clobbered by a full overwrite.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from wilbur.config import Config
from wilbur.tools import build_registry
from wilbur.tools.base import Approval, ToolContext


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda *_: Approval.GRANTED)


@pytest.fixture
def tools():
    return build_registry()


def read(tools, ctx, path):
    return tools["read_file"].run({"path": str(path)}, ctx)


def bump_mtime(path: Path):
    """Force a distinct mtime regardless of filesystem timestamp resolution."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))


def test_write_after_external_change_is_refused(tmp_path, ctx, tools):
    f = tmp_path / "f.txt"
    f.write_text("original\n")
    read(tools, ctx, f)
    f.write_text("changed by someone else\n")   # concurrent edit
    bump_mtime(f)
    res = tools["write_file"].run({"path": str(f), "content": "mine\n"}, ctx)
    assert res.is_error and "changed on disk" in res.output
    assert f.read_text() == "changed by someone else\n"   # not clobbered


def test_write_after_own_read_is_allowed(tmp_path, ctx, tools):
    f = tmp_path / "f.txt"
    f.write_text("original\n")
    read(tools, ctx, f)
    res = tools["write_file"].run({"path": str(f), "content": "mine\n"}, ctx)
    assert not res.is_error
    assert f.read_text() == "mine\n"


def test_write_then_write_again_is_allowed(tmp_path, ctx, tools):
    """A write updates the recorded mtime, so a second write is not stale."""
    f = tmp_path / "f.txt"
    f.write_text("v0\n")
    read(tools, ctx, f)
    tools["write_file"].run({"path": str(f), "content": "v1\n"}, ctx)
    res = tools["write_file"].run({"path": str(f), "content": "v2\n"}, ctx)
    assert not res.is_error and f.read_text() == "v2\n"


def test_edit_after_external_change_is_refused(tmp_path, ctx, tools):
    f = tmp_path / "f.txt"
    f.write_text("alpha\nbeta\n")
    read(tools, ctx, f)
    f.write_text("alpha\nbeta\ngamma\n")
    bump_mtime(f)
    res = tools["edit_file"].run(
        {"path": str(f), "old_string": "alpha", "new_string": "ALPHA"}, ctx)
    assert res.is_error and "changed on disk" in res.output


def test_new_file_write_is_not_blocked(tmp_path, ctx, tools):
    """A file that never existed has no recorded mtime and writes freely."""
    f = tmp_path / "new.txt"
    res = tools["write_file"].run({"path": str(f), "content": "hello\n"}, ctx)
    assert not res.is_error and f.read_text() == "hello\n"
