"""Tests for wilbur/progress.py: the append-only per-session progress log.

No network, no GPU, and no real ~/.config/wilbur or ~/.local: the log
directory is redirected into tmp_path via monkeypatch (the autouse
`isolate_wilbur_home` fixture in conftest.py does the same, and these tests
patch it explicitly so they stay correct even if that fixture changes).
"""
from pathlib import Path

import wilbur.config as config_mod
import wilbur.progress as progress


def _entries(path: Path) -> list[str]:
    """The bullet lines of a progress log, one per appended event."""
    return [line for line in path.read_text().splitlines() if line.startswith("- ")]


def test_log_path_contains_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "SESSION_DIR", tmp_path / "sessions")
    path = progress.log_path("abc123")
    assert isinstance(path, Path)
    assert "abc123" in str(path)
    assert path.name == "abc123.progress.md"
    assert path.parent == tmp_path / "sessions"


def test_append_returns_path_and_writes_event_and_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "SESSION_DIR", tmp_path / "sessions")
    path = progress.append("s1", "started", "first round")
    assert path == progress.log_path("s1")
    assert path.exists()
    content = path.read_text()
    assert "started" in content
    assert "first round" in content


def test_two_appends_leave_two_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "SESSION_DIR", tmp_path / "sessions")
    path = progress.append("s2", "checkpoint", "round cap reached")
    progress.append("s2", "stopped", "user interrupted")
    entries = _entries(path)
    assert len(entries) == 2
    assert "checkpoint" in entries[0]
    assert "round cap reached" in path.read_text()
    assert "stopped" in entries[1]
    assert "user interrupted" in path.read_text()
