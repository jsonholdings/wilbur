"""wilbur/render.py: streamed markdown, highlighted fences, tables, diffs,
and the display-only wiring (session text stays raw)."""
import re
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import wilbur.render as render
import wilbur.ui as ui
from wilbur.highlight import Highlighter

ANSI = re.compile(r"\x1b\][^\x1b\a]*(?:\x1b\\|\a)|\x1b\[[0-9;]*m")


def strip(text: str) -> str:
    return ANSI.sub("", text)


@pytest.fixture
def colour(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", False)


@pytest.fixture
def plain(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", True)


REPLY = ("# Plan\n\nSome **bold** and *it* and `x = 1`.\n\n"
         "```python\ndef f():\n    return \"s\"  # c\n```\n\n- one\n2. two\n")


def test_no_color_is_byte_identical(plain):
    assert render.markdown(REPLY) == REPLY
    assert ui.assistant("  hi **there**  ") == "hi **there**"
    s = render.MarkdownStream()
    assert s.feed(REPLY[:7]) + s.feed(REPLY[7:]) + s.flush() == REPLY
    assert render.diff_lines("a\n", "b\n") == ["1 - a", "1 + b"]


def test_fence_body_is_highlighted_like_the_lexer(colour):
    out = render.markdown("```python\ndef f():\n```")
    hl = Highlighter("python")
    assert hl.line("def f():") in out.splitlines()


@pytest.mark.parametrize("cut", range(1, len(REPLY)))
def test_fence_split_at_every_point_renders_identically(colour, cut):
    whole = render.markdown(REPLY)
    s = render.MarkdownStream()
    streamed = (s.feed(REPLY[:cut]) + s.feed(REPLY[cut:]) + s.flush()).rstrip("\n")
    assert streamed == whole


def test_stream_char_by_char_matches_whole(colour):
    s = render.MarkdownStream()
    streamed = "".join(s.feed(ch) for ch in REPLY) + s.flush()
    assert streamed.rstrip("\n") == render.markdown(REPLY)


def test_partial_line_waits_for_newline(colour):
    s = render.MarkdownStream()
    assert s.feed("```py") == ""
    assert s.feed("thon\nx") != ""          # the fence line is complete now
    assert "x" in s.flush()


def test_unclosed_fence_flushes_as_code(colour):
    out = render.markdown("```js\nconst a = 1")
    assert strip(out) == "```js\nconst a = 1"
    assert Highlighter("js").line("const a = 1") in out


def test_markdown_styles_but_keeps_text(colour):
    out = render.markdown(REPLY)
    text = strip(out)
    assert "Plan" in text and "#" not in text.splitlines()[0]
    assert "\x1b[1m" + "bold" in out
    assert "\x1b[3m" + "it" in out
    assert render.CODE + "x = 1" in out
    assert "• one" in text
    assert "2. two" in text
    assert "**" not in text


def test_markdown_inside_code_is_left_alone(colour):
    out = strip(render.markdown("```\n**not bold** # nope\n- item\n```"))
    assert "**not bold** # nope" in out and "- item" in out


def test_link_is_osc8_with_url_shown(colour):
    out = render.markdown("see [docs](https://x.io/a)")
    assert "\x1b]8;;https://x.io/a\x1b\\" in out
    assert strip(out) == "see docs (https://x.io/a)"
    assert ui.visible_width(out) == len("see docs (https://x.io/a)")


def test_table_columns_align_with_wide_characters(colour):
    table = "| name | v |\n|---|---|\n| 日本語 | **1** |\n| a | 22 |\n"
    rows = [r for r in render.markdown(table).splitlines()]
    widths = {ui.visible_width(r) for r in rows}
    assert len(widths) == 1, rows
    bars = [[i for i, ch in enumerate(_cells(strip(r))) if ch == "│"] for r in rows if "│" in r]
    assert all(b == bars[0] for b in bars)


def _cells(text: str) -> list[str]:
    """Expand wide characters to two cells so bar positions compare by cell."""
    out = []
    for ch in text:
        out.append(ch)
        if ui._cell_width(ch) == 2:
            out.append("")
    return out


def test_table_at_end_of_stream_is_flushed(colour):
    s = render.MarkdownStream()
    out = s.feed("| a | b |\n| 1 | 2 |") + s.flush()
    assert "│" in out and "1" in strip(out) and "2" in strip(out)


def test_very_long_line_is_kept_whole(colour):
    line = "word " * 5000
    assert strip(render.markdown(line)) == line


def test_render_speed_2000_line_reply(colour):
    body = "\n".join(f"    x_{i} = compute({i}, 'v')  # note {i}" for i in range(2000))
    text = "Intro **here**\n```python\n" + body + "\n```\n"
    best = min(_timed(render.markdown, text) for _ in range(3))
    assert best < 0.05, f"{best * 1000:.1f} ms"


def _timed(fn, arg) -> float:
    t = time.perf_counter()
    fn(arg)
    return time.perf_counter() - t


# ------------------------------------------------------------------ diffs

def test_diff_counts_and_summary(plain):
    assert render.diff_counts("a\nb\nc\n", "a\nB\nc\nd\n") == (2, 1)
    assert render.diff_summary(2, 1) == "2 additions and 1 removal  +2 −1"
    assert render.diff_summary(1, 0) == "1 addition and 0 removals  +1 −0"


def test_diff_lines_numbers_and_signs(plain):
    rows = render.diff_lines("a\nb\nc\n", "a\nB\nc\n", start=10)
    assert rows == ["10   a", "11 - b", "11 + B", "12   c"]


def test_diff_lines_colours(colour):
    rows = render.diff_lines("x\n", "y\n")
    assert render.DEL_BG in rows[0] and render.DEL_FG in rows[0] and "- x" in rows[0]
    assert render.ADD_BG in rows[1] and render.ADD_FG in rows[1] and "+ y" in rows[1]


def test_diff_distant_hunks_are_separated(plain):
    old = "\n".join(str(i) for i in range(30))
    new = old.replace("2\n", "two\n", 1).replace("27", "twenty-seven")
    rows = render.diff_lines(old, new, context=1)
    assert any(r.strip() == "..." for r in rows)
    assert not any(r.endswith(" 15") for r in rows)


def test_diff_is_capped(plain):
    rows = render.diff_lines("", "\n".join("l" for _ in range(100)), max_lines=10)
    assert len(rows) == 11 and "+90 more diff lines" in rows[-1]


def test_diff_view_header(plain):
    out = render.diff_view("a.py", "", "x\ny\n", created=True)
    first = out.splitlines()[0]
    assert "Created a.py with 2 additions and 0 removals  +2 −0" in first
    assert out.splitlines()[1].strip() == "1 + x"


def test_edit_tool_reports_display_diff_but_model_text_unchanged(tmp_path):
    from wilbur.tools.base import Approval, ToolContext
    from wilbur.tools.files import EditFile, ReadFile, WriteFile

    class Cfg:
        max_read_lines = 2000
        max_tool_output_chars = 100000
        max_tool_output_lines = 2000

    ctx = ToolContext(cwd=str(tmp_path), config=Cfg(), read_files={},
                      approve=lambda *a: Approval.GRANTED)
    f = tmp_path / "m.py"
    f.write_text("a = 1\nb = 2\nc = 3\n")
    ReadFile().run({"path": "m.py"}, ctx)
    res = EditFile().run({"path": "m.py", "old_string": "b = 2", "new_string": "b = 20"}, ctx)
    assert res.output.startswith("Edited ") and "b = 20" not in res.output
    assert res.diff == {"path": "m.py", "old": "b = 2", "new": "b = 20",
                        "start": 2, "created": False}
    res = WriteFile().run({"path": "n.py", "content": "z\n"}, ctx)
    assert res.diff["created"] is True and res.diff["old"] == ""
    assert "z" not in res.output.replace(str(tmp_path), "")


def test_file_view_highlights_after_gutter(colour):
    lines = ["     1\tdef f():", "     2\t    return 1"]
    out = render.file_view_lines(lines, "x.py")
    assert [strip(line) for line in out] == lines
    assert Highlighter("python").line("def f():") in out[0]
    assert render.file_view_lines(lines, "x.unknownext") == lines


def test_status_bar(plain):
    bar = ui.status_bar("qwen", 4096, 16384, 9000, 500, 10.0, "idle")
    assert bar == ("model: qwen  ·  context 25%  ·  tokens: 9,000 in / 500 out"
                   "  ·  50 tok/s  ·  idle")
    assert "-- tok/s" in ui.status_bar("m", 0, 0, 0, 0, 0.0)


def test_visible_width_skips_osc8():
    s = "\x1b]8;;http://a.b/c\x1b\\" + "\x1b[4mlabel\x1b[0m" + "\x1b]8;;\x1b\\"
    assert ui.visible_width(s) == 5


def test_nested_inline_code_in_bold(colour):
    out = render.markdown("use **`render` now** ok")
    assert strip(out) == "use render now ok"
    assert render.CODE + "render" in out and out.count("\x1b[1m") >= 2
