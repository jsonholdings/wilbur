"""Tests for wilbur/highlight.py."""
import re
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import wilbur.ui as ui
from wilbur import highlight as hl

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def _colour(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", False)


def strip(s):
    return ANSI.sub("", s)


def painted(out, token, code):
    return f"{code}{token}{hl.RESET}" in out


@pytest.mark.parametrize("tag,want", [
    ("python", "python"), ("py", "python"), ("ts", "js"), ("JavaScript", "js"),
    ("bash", "shell"), ("console", "shell"), ("zsh", "shell"), ("yml", "yaml"),
    ("c++", "c"), ("hpp", "c"), ("c#", "csharp"), ("cs", "csharp"), ("md", "markdown"),
    ("patch", "diff"), ("sql", "sql"), ("php", "php"), ("json", "json"),
    ("cobol", None), (None, None), ("", None),
])
def test_normalize_lang(tag, want):
    assert hl.normalize_lang(tag) == want


@pytest.mark.parametrize("path,want", [
    ("a/b.py", "python"), ("x.TSX", "js"), ("run.sh", "shell"), ("c.yml", "yaml"),
    ("m.hpp", "c"), ("p.cs", "csharp"), ("fix.patch", "diff"), ("README.md", "markdown"),
    ("Makefile", None), ("noext", None),
])
def test_lang_for_path(path, want):
    assert hl.lang_for_path(path) == want


def test_python_tokens():
    out = hl.highlight('def f(x): return len("hi") + 42  # note', "python")
    assert painted(out, "def", hl.KEYWORD)
    assert painted(out, "return", hl.KEYWORD)
    assert painted(out, "len", hl.BUILTIN)
    assert painted(out, '"hi"', hl.STRING)
    assert painted(out, "42", hl.NUMBER)
    assert painted(out, "# note", hl.COMMENT)


def test_python_hash_in_string_is_not_comment():
    out = hl.highlight('s = "a # b"', "python")
    assert painted(out, '"a # b"', hl.STRING)
    assert hl.COMMENT not in out


def test_python_triple_quote_state():
    h = hl.Highlighter("python")
    l1 = h.line('x = """start')
    l2 = h.line("if def return 5")
    l3 = h.line('end""" + 1')
    assert painted(l1, '"""', hl.STRING) and painted(l1, "start", hl.STRING)
    assert l2 == f"{hl.STRING}if def return 5{hl.RESET}"
    assert painted(l3, 'end"""', hl.STRING)
    assert painted(l3, "1", hl.NUMBER)


def test_python_triple_quote_state_tracked_under_no_color(monkeypatch):
    h = hl.Highlighter("python")
    monkeypatch.setattr(ui, "_NO_COLOR", True)
    h.line('"""open')
    monkeypatch.setattr(ui, "_NO_COLOR", False)
    assert h.line("still string") == f"{hl.STRING}still string{hl.RESET}"


def test_c_block_comment_state():
    h = hl.Highlighter("c")
    h.line("int x; /* begin")
    mid = h.line("int return 3")
    end = h.line("done */ return 0;")
    assert mid == f"{hl.COMMENT}int return 3{hl.RESET}"
    assert painted(end, "done */", hl.COMMENT)
    assert painted(end, "return", hl.KEYWORD)


def test_js_tokens():
    out = hl.highlight("const s = `t${1}`; // c\nlet n = 0x1F; console.log('a')", "js")
    assert painted(out, "const", hl.KEYWORD)
    assert painted(out, "`t${1}`", hl.STRING)
    assert painted(out, "// c", hl.COMMENT)
    assert painted(out, "0x1F", hl.NUMBER)
    assert painted(out, "console", hl.BUILTIN)


def test_js_block_comment_across_lines():
    out = hl.highlight("/* a\nb\n*/ var", "ts").split("\n")
    assert out[1] == f"{hl.COMMENT}b{hl.RESET}"
    assert painted(out[2], "var", hl.KEYWORD)


def test_shell_tokens():
    out = hl.highlight('if [ -n "$X" ]; then echo $HOME; fi # done', "bash")
    assert painted(out, "if", hl.KEYWORD)
    assert painted(out, '"$X"', hl.STRING)
    assert painted(out, "echo", hl.BUILTIN)
    assert painted(out, "$HOME", hl.BUILTIN)
    assert painted(out, "# done", hl.COMMENT)


def test_json_keys_vs_values():
    out = hl.highlight('{"name": "wilbur", "n": 3, "ok": true}', "json")
    assert painted(out, '"name"', hl.KEY)
    assert painted(out, '"wilbur"', hl.STRING)
    assert painted(out, "3", hl.NUMBER)
    assert painted(out, "true", hl.BUILTIN)


def test_yaml_keys():
    out = hl.highlight("name: wilbur  # c\ncount: 3\nok: true", "yaml")
    assert painted(out, "name", hl.KEY)
    assert painted(out, "# c", hl.COMMENT)
    assert painted(out, "3", hl.NUMBER)
    assert painted(out, "true", hl.BUILTIN)


def test_csharp_and_php_and_sql():
    cs = hl.highlight('public async Task Run() { var s = @"x"; }', "c#")
    assert painted(cs, "public", hl.KEYWORD) and painted(cs, '@"x"', hl.STRING)
    php = hl.highlight('<?php echo $name; // hi', "php")
    assert painted(php, "echo", hl.KEYWORD) and painted(php, "$name", hl.BUILTIN)
    assert painted(php, "// hi", hl.COMMENT)
    sql = hl.highlight("SELECT id FROM t WHERE x = 'a' -- c", "sql")
    assert painted(sql, "SELECT", hl.KEYWORD) and painted(sql, "where".upper(), hl.KEYWORD)
    assert painted(sql, "'a'", hl.STRING) and painted(sql, "-- c", hl.COMMENT)


def test_diff_lines():
    out = hl.highlight("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n same", "diff").split("\n")
    assert out[0] == f"{hl.HEADER}--- a/x{hl.RESET}"
    assert out[2] == f"{hl.HUNK}@@ -1 +1 @@{hl.RESET}"
    assert out[3] == f"{hl.REMOVED}-old{hl.RESET}"
    assert out[4] == f"{hl.ADDED}+new{hl.RESET}"
    assert out[5] == " same"


def test_markdown_heading_and_inline_code():
    out = hl.highlight("# Title\nuse `pip` now", "markdown").split("\n")
    assert out[0] == f"{hl.HEADING}# Title{hl.RESET}"
    assert painted(out[1], "`pip`", hl.CODE)


def test_unknown_language_passthrough():
    assert hl.highlight("def x", "cobol") == "def x"
    assert hl.highlight("def x", None) == "def x"


ODD = [
    "", "\n\n", 'x = "unterminated', "s = 'esc \\' q' + \"d\\\"q\"", "\tif\ttrue:\t# tab",
    "名前 = '日本語' # 😀 emoji", "a\r\nb\rc\n", "/* never closed", '"""\nnope',
    "x" * 20000, "-" * 5 + "+" * 5, "``` code\n`a", "key: 'v' # c\n- item: 1",
]
LANGS = ["python", "js", "shell", "json", "yaml", "c", "csharp", "php", "sql",
         "markdown", "diff", None, "nope"]


@pytest.mark.parametrize("lang", LANGS)
def test_round_trip_strip_ansi(lang):
    for s in ODD:
        assert strip(hl.highlight(s, lang)) == s


@pytest.mark.parametrize("lang", LANGS)
def test_no_color_is_byte_identical(lang, monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", True)
    for s in ODD + ['def f(): return "x" # c']:
        assert hl.highlight(s, lang) == s


def _best_of_3(code, lang):
    best = float("inf")
    for _ in range(3):
        t = time.perf_counter()
        hl.highlight(code, lang)
        best = min(best, time.perf_counter() - t)
    return best


def test_speed_python_2000_lines():
    code = "\n".join(f'def f{i}(x, y=[1, 2]):  # c{i}\n    return len("s{i}") + 0x{i:x}'
                     for i in range(1000))
    hl.highlight("x", "python")
    took = _best_of_3(code, "python")
    print(f"python 2000 lines: {took*1000:.1f} ms")
    assert took < 0.05


def test_speed_js_2000_lines():
    code = "\n".join(f"const v{i} = await fetch('u{i}'); // c\nif (v{i}) {{ return {i}; }}"
                     for i in range(1000))
    hl.highlight("x", "js")
    took = _best_of_3(code, "js")
    print(f"js 2000 lines: {took*1000:.1f} ms")
    assert took < 0.05
