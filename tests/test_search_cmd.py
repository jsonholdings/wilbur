"""Grep command construction for both backends.

Only the grep fallback runs in this environment (no rg), and existing tests
cover its behaviour. These assert the *argv* both branches build -- so the rg
path (which cannot run here) is at least pinned to intent, and both backends
stay consistent about excluding the noise dirs and honouring the flags.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import wilbur.tools.search as search_mod
from wilbur.config import Config
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.search import SKIP_DIRS, Grep


class CapturedRun:
    """Stands in for subprocess.run; records argv and returns an empty result."""
    def __init__(self):
        self.cmd = None

    def __call__(self, cmd, **kw):
        self.cmd = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda *_: Approval.GRANTED)


def argv_for(monkeypatch, ctx, backend, args):
    run = CapturedRun()
    monkeypatch.setattr(subprocess, "run", run)
    # force which() to find, or not find, rg
    monkeypatch.setattr(search_mod.shutil, "which",
                        lambda name: "/usr/bin/rg" if backend == "rg" else None)
    Grep().run(args, ctx)
    return run.cmd


# --------------------------------------------------------------------- #
# rg branch


def test_rg_excludes_every_skip_dir(monkeypatch, ctx):
    cmd = argv_for(monkeypatch, ctx, "rg", {"pattern": "foo"})
    joined = " ".join(cmd)
    for d in SKIP_DIRS:
        assert f"!**/{d}/**" in joined, f"{d} not excluded in rg argv"


def test_rg_user_glob_precedes_exclusions(monkeypatch, ctx):
    cmd = argv_for(monkeypatch, ctx, "rg", {"pattern": "foo", "glob": "*.py"})
    gi = cmd.index("*.py")
    first_excl = cmd.index("!**/.git/**")
    assert gi < first_excl  # user glob first, exclusions after (last-wins)


def test_rg_passes_ignore_case_and_files_only(monkeypatch, ctx):
    cmd = argv_for(monkeypatch, ctx, "rg",
                   {"pattern": "foo", "ignore_case": True, "files_only": True})
    assert "-i" in cmd and "--files-with-matches" in cmd


def test_rg_pattern_is_after_the_double_dash(monkeypatch, ctx):
    """-- guards against a pattern that looks like a flag."""
    cmd = argv_for(monkeypatch, ctx, "rg", {"pattern": "-x"})
    assert "--" in cmd and cmd.index("--") < cmd.index("-x")


# --------------------------------------------------------------------- #
# grep fallback


def test_grep_excludes_every_skip_dir(monkeypatch, ctx):
    cmd = argv_for(monkeypatch, ctx, "grep", {"pattern": "foo"})
    for d in SKIP_DIRS:
        assert d in cmd  # each appears as an --exclude-dir value
    assert cmd.count("--exclude-dir") == len(SKIP_DIRS)


def test_grep_uses_dash_e_for_the_pattern(monkeypatch, ctx):
    """grep -e keeps a pattern starting with - from parsing as a flag."""
    cmd = argv_for(monkeypatch, ctx, "grep", {"pattern": "-x"})
    assert "-e" in cmd and cmd.index("-e") < cmd.index("-x")


def test_grep_include_glob_is_passed(monkeypatch, ctx):
    cmd = argv_for(monkeypatch, ctx, "grep", {"pattern": "foo", "glob": "*.py"})
    assert "--include" in cmd and "*.py" in cmd
