"""Startup wordmark: sizing, colour, and visible-width accounting."""
from __future__ import annotations

from wilbur import art


def test_wordmark_wide_uses_full_block_letter_art():
    """A wide terminal gets the full four-line wordmark, not the compact one."""
    out = art.wordmark(width=100)
    lines = out.splitlines()
    assert len(lines) == 4
    # The compact fallback carries the spaced-out name; the full art does not.
    assert "W I L B U R" not in out
    # Colour mode wraps every line in an ANSI SGR sequence.
    assert "\033[38;5;" in out


def test_wordmark_narrow_uses_compact_fallback():
    """A narrow terminal gets the three-line compact art instead."""
    out = art.wordmark(width=40)
    lines = out.splitlines()
    assert len(lines) == 3
    assert "W I L B U R" in out


def test_wordmark_without_colour_has_no_ansi_escapes():
    for width in (100, 40):
        out = art.wordmark(width=width, colour=False)
        assert "\033" not in out
        assert out == art.wordmark(width=width, colour=False)


def test_glyph_line_keeps_the_text():
    plain = art.glyph_line("hello world", colour=False)
    assert plain == f"{art.GLYPH} hello world"
    coloured = art.glyph_line("hello world")
    assert "hello world" in coloured
    assert art.GLYPH in coloured
    assert "\033" in coloured


def test_visible_width_ignores_ansi_codes():
    plain = "hello"
    coloured = f"\033[38;5;130m{plain}\033[0m"
    assert art._visible_width(coloured) == len(plain)
    # Widest line wins, escapes on every line are ignored.
    multi = f"\033[1mshort\033[0m\n\033[38;5;209m{'x' * 42}\033[0m"
    assert art._visible_width(multi) == 42
    # No escapes at all: plain character count.
    assert art._visible_width("ab\ncd") == 2
