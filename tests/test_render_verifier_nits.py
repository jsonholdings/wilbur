"""BACKLOG terminal-ux-parity-with-claude-code, spec item 10 (verifier nits,
2.42.0): (a) an unknown-extension read_file view must render grey, not
unstyled; (b) a 4-backtick fence must not close on a 3-backtick line.
Both with colour on and with NO_COLOR (byte-identical output)."""
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import wilbur.render as render
import wilbur.ui as ui

ANSI = re.compile(r"\x1b\][^\x1b\a]*(?:\x1b\\|\a)|\x1b\[[0-9;]*m")


def strip(text: str) -> str:
    return ANSI.sub("", text)


@pytest.fixture
def colour(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", False)


@pytest.fixture
def plain(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", True)


def test_file_view_unknown_extension_is_grey(colour):
    lines = ["     1\tfoo bar", "     2\tbaz"]
    out = render.file_view_lines(lines, "x.unknownext")
    assert [strip(line) for line in out] == lines
    assert out[0] == ui.c("     1\t", ui.GREY) + ui.c("foo bar", ui.GREY)
    assert out[1] == ui.c("     2\t", ui.GREY) + ui.c("baz", ui.GREY)


def test_file_view_unknown_extension_plain_is_byte_identical(plain):
    lines = ["     1\tfoo bar", "     2\tbaz"]
    assert render.file_view_lines(lines, "x.unknownext") == lines
    assert render.file_view_lines(lines, "x.py") == lines


def test_four_backtick_fence_not_closed_by_three(colour):
    text = "````\n```\nstill code\n````"
    out = render.markdown(text)
    assert strip(out) == text
    # the 3-backtick line stays inside the fence: dimmed, not highlighted
    assert ui.DIM + "```" in out
    # and the fence is still open after it: the body line is code, not prose
    assert "still code" in out
    assert ui.DIM + "````" in out


def test_four_backtick_fence_plain_is_byte_identical(plain):
    text = "````\n```\nstill code\n````"
    assert render.markdown(text) == text
    s = render.MarkdownStream()
    assert s.feed(text[:5]) + s.feed(text[5:]) + s.flush() == text
