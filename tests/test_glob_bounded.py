"""glob-unbounded-walk: the walk prunes SKIP_DIRS, stops at a match cap and a
time budget, and says when its result is partial."""
from __future__ import annotations

import os

from wilbur.config import Config
from wilbur.tools import search
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.search import Glob


def _ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


def _deep_tree(root, dirs=30, files=10):
    for d in range(dirs):
        sub = root / f"d{d}" / "x"
        sub.mkdir(parents=True)
        for f in range(files):
            (sub / f"f{f}.py").write_text("")


def test_skip_dir_is_never_entered(tmp_path, monkeypatch):
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "a.py").write_text("")
    (tmp_path / "keep.py").write_text("")
    entered = []
    real_walk = os.walk

    def spy(top, *a, **k):
        for dirpath, dirnames, filenames in real_walk(top, *a, **k):
            entered.append(dirpath)
            yield dirpath, dirnames, filenames
    monkeypatch.setattr(search.os, "walk", spy)
    out = Glob().run({"pattern": "**/*.py"}, _ctx(tmp_path))
    assert str(tmp_path / "keep.py") in out.output
    assert "node_modules" not in out.output
    assert not any("node_modules" in p for p in entered), entered


def test_match_cap_cuts_and_reports(tmp_path, monkeypatch):
    _deep_tree(tmp_path)
    monkeypatch.setattr(search, "GLOB_MAX_MATCHES", 25)
    out = Glob().run({"pattern": "**/*.py"}, _ctx(tmp_path))
    assert out.output.count(".py") == 25
    assert "partial result" in out.output and "25-match cap" in out.output


def test_time_budget_cuts_and_reports(tmp_path, monkeypatch):
    _deep_tree(tmp_path)
    monkeypatch.setattr(search, "GLOB_TIME_BUDGET_S", -1.0)  # already past
    out = Glob().run({"pattern": "**/*.py"}, _ctx(tmp_path))
    assert "partial result" in out.output and "time budget" in out.output
    assert out.output.count(".py") < 300


def test_control_full_walk_is_not_marked_partial(tmp_path):
    _deep_tree(tmp_path)
    out = Glob().run({"pattern": "**/*.py"}, _ctx(tmp_path))
    assert "partial result" not in out.output
    assert out.output.count(".py") == 300
