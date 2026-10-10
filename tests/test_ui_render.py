"""Public render helpers in wilbur/ui.py not covered by test_ui_helpers.py:
tool_call, signoff, banner.

_NO_COLOR is forced on so c() returns plain text; the assertions then hold
regardless of how pytest is invoked, and the no-ANSI-escape checks below are
meaningful (with colour on, c() would wrap every fragment in \x1b sequences).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import wilbur.ui as ui


@pytest.fixture(autouse=True)
def _plain(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", True)


def test_tool_call_renders_name_and_summary_without_ansi():
    out = ui.tool_call("run_bash", "ls -la")
    assert "run_bash" in out
    assert "(ls -la)" in out
    assert ui.BULLET in out
    assert "\x1b" not in out


def test_tool_call_marks_recovered_calls():
    out = ui.tool_call("read_file", "src/main.py", recovered=True)
    assert "(recovered)" in out
    assert "\x1b" not in out


def test_signoff_returns_plain_text_with_colour_off():
    out = ui.signoff("done")
    assert out == "done"
    assert "\x1b" not in out


def test_banner_contains_session_details_without_ansi():
    out = ui.banner("qwen2.5:7b", "/srv/projects/team/wilbur", 8192, "fast")
    assert "Wilbur" in out
    assert "qwen2.5:7b" in out
    assert "8,192 tokens" in out
    assert "fast" in out
    assert "\x1b" not in out
