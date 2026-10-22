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


def _box_rows(out):
    return [line for line in out.splitlines() if line[:1] in ("╭", "│", "╰")]


@pytest.mark.parametrize("colour", [False, True])
@pytest.mark.parametrize("model,cwd", [
    ("huihui_ai/qwen3.8-abliterated:27b", "/home/user"),
    ("模型-宽字符:27b", "/home/user/项目"),
])
def test_banner_box_rows_share_one_cell_width(monkeypatch, colour, model, cwd):
    # Owner screenshot 2026-10-21 23:17: the first row's right border jutted out
    # one cell, because the marker drew 2 cells while padding counted 1.
    monkeypatch.setattr(ui, "_NO_COLOR", not colour)
    rows = _box_rows(ui.banner(model, cwd, 65536))
    if colour:
        rows = [line for line in ui.banner(model, cwd, 65536).splitlines()
                if any(ch in line for ch in "╭│╰")]
    assert len(rows) == 7
    assert len({ui.visible_width(r) for r in rows}) == 1


def test_banner_box_avoids_terminal_dependent_marker():
    # U+23FA is an emoji in some terminals and text in others; no single
    # padding is right for both, so it must not appear inside the box.
    rows = _box_rows(ui.banner("m", "/tmp", 1))
    assert not any(ui.BULLET in r for r in rows)
    assert ui.BOX_MARK in rows[1]


def test_visible_width_counts_wide_zero_and_escapes():
    assert ui.visible_width("abc") == 3
    assert ui.visible_width("宽") == 2
    assert ui.visible_width("é") == 1
    assert ui.visible_width("\033[38;5;209mab\033[0m") == 2
