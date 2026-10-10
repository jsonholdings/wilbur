"""Pure helpers in wilbur/ui.py: _shorten, _ansi_pad, tool_result.

No network, no GPU, no TTY: _NO_COLOR is forced on so c() returns plain
text and the assertions below hold regardless of how pytest is invoked.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import wilbur.ui as ui


@pytest.fixture(autouse=True)
def _plain(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", True)


def test_shorten_returns_text_unchanged_within_limit():
    assert ui._shorten("hello", 10) == "hello"
    assert ui._shorten("hello", 5) == "hello"


def test_shorten_respects_limit():
    text = "a" * 50
    for limit in (10, 20, 30):
        out = ui._shorten(text, limit)
        assert len(out) == limit
        assert out.startswith("...")
        assert out.endswith(text[-(limit - 3):])


def test_shorten_keeps_the_tail_of_a_path():
    path = "/srv/projects/team/wilbur"
    out = ui._shorten(path, 15)
    assert out == "..." + path[-12:]


def test_ansi_pad_is_zero_for_plain_text():
    assert ui._ansi_pad("hello") == 0
    assert ui._ansi_pad("") == 0


def test_ansi_pad_counts_only_escape_characters():
    coloured = ui.ACCENT + "hello" + ui.RESET
    assert ui._ansi_pad(coloured) == len(coloured) - len("hello")
    # Padding a coloured string by that amount lands exactly on the
    # visible width, the way banner() relies on it.
    assert (coloured.ljust(len("hello") + ui._ansi_pad(coloured))).rstrip() == coloured


def test_tool_result_truncates_output_longer_than_max_lines():
    output = "\n".join(f"line {i}" for i in range(10))
    out = ui.tool_result(output, max_lines=3)
    assert "line 0" in out and "line 2" in out
    assert "line 3" not in out
    assert "+7 lines" in out


def test_tool_result_leaves_short_output_alone():
    out = ui.tool_result("only one line", max_lines=6)
    assert "only one line" in out
    assert "+1 lines" not in out


def test_tool_result_reports_empty_output():
    assert "(no output)" in ui.tool_result("", max_lines=6)
