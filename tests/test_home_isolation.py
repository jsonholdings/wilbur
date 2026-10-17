"""The suite can never write into the owner's real Wilbur home (2.35.2)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import conftest

REAL_HOME_DIRS = (Path.home() / ".local" / "share" / "wilbur", Path.home() / ".config" / "wilbur")


def _under_real_home(path: str) -> bool:
    resolved = Path(path).resolve()
    return any(resolved == d or d in resolved.parents for d in REAL_HOME_DIRS)


def test_env_paths_point_at_a_throwaway_dir():
    for name in ("WILBUR_CONFIG", "WILBUR_SESSIONS", "WILBUR_HISTORY", "WILBUR_USER_SKILLS_DIR"):
        assert not _under_real_home(os.environ[name]), name


def test_a_fresh_import_defaults_away_from_the_real_sessions_dir():
    # What a thread that outlives its test's monkeypatch, or a spawned
    # subprocess, would fall back to.
    out = subprocess.run(
        [sys.executable, "-c", "import wilbur.config as c; print(c.SESSION_DIR); print(c.CONFIG_PATH)"],
        capture_output=True, text=True, check=True, cwd=Path(__file__).resolve().parents[1],
    ).stdout.split()
    assert len(out) == 2
    assert not any(_under_real_home(p) for p in out), out


def test_leak_guard_sees_a_test_named_session(monkeypatch, tmp_path):
    # Control for the session-wide guard: it must report a positive.
    monkeypatch.setattr(conftest, "_REAL_SESSIONS_DIR", tmp_path)
    assert conftest._test_named_sessions() == set()
    (tmp_path / "test_x0-1.progress.md").write_text("leak")
    (tmp_path / "desktopuser1-1.json").write_text("{}")  # the owner's own run: ignored
    assert conftest._test_named_sessions() == {"test_x0-1.progress.md"}
