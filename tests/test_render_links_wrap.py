"""U4 (2.66.0): OSC 8 file:// links on path:line, and prose word-wrapped at
ui.width() with hanging indents for list items. Display only; NO_COLOR output
stays byte-identical."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import wilbur.render as render
import wilbur.ui as ui

ANSI = re.compile(r"\x1b\][^\x1b\a]*(?:\x1b\\|\a)|\x1b\[[0-9;]*m")
OSC8 = re.compile(r"\x1b\]8;;([^\x1b]*)\x1b\\(.*?)\x1b\]8;;\x1b\\")


def strip(text: str) -> str:
    return ANSI.sub("", text)


@pytest.fixture
def colour(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", False)


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    monkeypatch.setattr(render, "link_root", str(tmp_path))
    return tmp_path


@pytest.fixture
def width(monkeypatch):
    def set_width(n: int) -> None:
        monkeypatch.setattr(ui, "width", lambda: n)
    return set_width


def test_existing_path_line_becomes_file_link(colour, project):
    out = render.markdown("The bug is at pkg/mod.py:12 in the loop.")
    [(url, label)] = OSC8.findall(out)
    assert url == (project / "pkg" / "mod.py").resolve().as_uri()
    assert strip(label) == "pkg/mod.py:12"
    assert strip(out) == "The bug is at pkg/mod.py:12 in the loop."


def test_path_line_col_and_inline_code_are_linked(colour, project):
    out = render.markdown("See `pkg/mod.py:3:7` and pkg/mod.py:4.")
    assert [strip(lbl) for _, lbl in OSC8.findall(out)] == ["pkg/mod.py:3:7", "pkg/mod.py:4"]


def test_missing_file_is_not_linked_control(colour, project):
    out = render.markdown("Not here: pkg/nope.py:3, nor a time 12:30, nor http://a.b/c.py:8")
    assert OSC8.findall(out) == []


def test_absolute_path_is_linked(colour, project):
    target = project / "pkg" / "mod.py"
    out = render.markdown(f"at {target}:9")
    assert OSC8.findall(out)[0][0] == target.resolve().as_uri()


def test_no_color_is_untouched(monkeypatch, project):
    monkeypatch.setattr(ui, "_NO_COLOR", True)
    text = "pkg/mod.py:1 " + "word " * 40
    assert render.markdown(text) == text


def test_prose_wraps_at_width(colour, width):
    width(40)
    out = strip(render.markdown("alpha " * 30)).split("\n")
    assert len(out) > 1 and all(len(row) <= 40 for row in out)


def test_short_line_is_not_wrapped_control(colour, width):
    width(40)
    assert "\n" not in render.markdown("short line")


def test_bullet_has_hanging_indent(colour, width):
    width(30)
    out = strip(render.markdown("- " + "beta " * 15)).split("\n")
    assert out[0].startswith("• beta")
    assert len(out) > 1 and all(row.startswith("  beta") for row in out[1:])
    assert all(len(row) <= 30 for row in out)


def test_numbered_and_nested_hanging_indent(colour, width):
    width(30)
    out = strip(render.markdown("  12. " + "gamma " * 12)).split("\n")
    assert out[0].startswith("  12. gamma")
    assert all(row.startswith("      gamma") for row in out[1:])


def test_inline_markup_is_not_split_across_lines(colour, width):
    width(24)
    out = render.markdown("aaaa bbbb cccc dddd **bold words here** eeee")
    assert "**" not in strip(out)
    assert "bold words here" in strip(out)


def test_quote_wraps_with_bar_on_every_line(colour, width):
    width(30)
    out = strip(render.markdown("> " + "delta " * 12)).split("\n")
    assert len(out) > 1 and all(row.startswith("▎") for row in out)
