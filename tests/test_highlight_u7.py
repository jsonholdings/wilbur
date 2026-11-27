"""U6/U7 highlighter tests: multi-line gaps and new languages, each with a control."""
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import wilbur.ui as ui
from wilbur import highlight as hl

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def _colour(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", False)


def painted(out, token, code):
    return f"{code}{token}{hl.RESET}" in out


def lines(lang, *src):
    h = hl.Highlighter(lang)
    return [h.line(s) for s in src]


# --- U6 -------------------------------------------------------------------
def test_js_multiline_template_literal():
    a, b, c = lines("js", "const s = `one", "two ${x} return", "end`; let y = 1;")
    assert painted(a, "`", hl.STRING) and painted(a, "one", hl.STRING)
    assert b == f"{hl.STRING}two ${{x}} return{hl.RESET}"
    assert painted(c, "end`", hl.STRING)
    assert painted(c, "let", hl.KEYWORD)


def test_js_template_escaped_backtick_does_not_close():
    a, b = lines("js", "x = `a \\` b", "c` + 1")
    assert b.startswith(f"{hl.STRING}c`{hl.RESET}")


def test_js_regex_literal():
    out = hl.highlight("const r = /ab+c\\/d[/]/gi;", "js")
    assert painted(out, " /ab+c\\/d[/]/gi", hl.STRING)


def test_js_division_is_not_regex_control():
    out = hl.highlight("let q = a / b / c;", "js")
    assert hl.STRING not in out


@pytest.mark.parametrize("ind", ["|", ">", "|-", ">+", "|2"])
def test_yaml_block_scalar(ind):
    a, b, c, d = lines("yaml", f"script: {ind}", "  echo true", "", "next: 1")
    assert b == f"{hl.STRING}  echo true{hl.RESET}"
    assert painted(d, "next", hl.KEY) and painted(d, "1", hl.NUMBER)


def test_yaml_pipe_in_value_is_not_block_control():
    a, b = lines("yaml", "cmd: a | b", "  other: true")
    assert painted(b, "true", hl.BUILTIN)


def test_cpp_raw_string():
    a, b, c = lines("cpp", 'auto s = R"x(one )" "', 'two', 'end)x"; int k;')
    assert painted(a, 'R"x(', hl.STRING) and painted(a, 'one )" "', hl.STRING)
    assert b == f"{hl.STRING}two{hl.RESET}"
    assert painted(c, 'end)x"', hl.STRING)
    assert painted(c, "int", hl.KEYWORD)


def test_cpp_plain_identifier_R_control():
    out = hl.highlight("int R = 2;", "cpp")
    assert hl.STRING not in out


# --- U7 -------------------------------------------------------------------
@pytest.mark.parametrize("tag,want", [
    ("go", "go"), ("rs", "rust"), ("rust", "rust"), ("java", "java"), ("toml", "toml"),
    ("dockerfile", "dockerfile"), ("html", "html"), ("css", "css"),
])
def test_aliases(tag, want):
    assert hl.normalize_lang(tag) == want


@pytest.mark.parametrize("path,want", [
    ("a/main.go", "go"), ("lib.rs", "rust"), ("A.java", "java"), ("Cargo.toml", "toml"),
    ("x/Dockerfile", "dockerfile"), ("Dockerfile.dev", "dockerfile"), ("i.html", "html"),
    ("s.css", "css"), ("Dockerfilex", None),
])
def test_paths(path, want):
    assert hl.lang_for_path(path) == want


def test_go():
    a, b = lines("go", "func main() { s := `raw", "\\n` ; x := len(nil) } // c")
    assert painted(a, "func", hl.KEYWORD)
    assert painted(b, "\\n`", hl.STRING)
    assert painted(b, "len", hl.BUILTIN) and painted(b, "// c", hl.COMMENT)
    assert not painted(a, "main", hl.KEYWORD)


def test_rust():
    out = hl.highlight('fn f<\'a>() { let s = r#"a"b"#; println!("x"); Some(1) }', "rust")
    assert painted(out, "fn", hl.KEYWORD) and painted(out, 'r#"a"b"#', hl.STRING)
    assert painted(out, "println!", hl.BUILTIN) and painted(out, "Some", hl.BUILTIN)
    assert hl.STRING + "'a" not in out   # lifetime is not a char literal


def test_java():
    a, b, c = lines("java", '@Override public String s = """', "body", '"""; int n = 3;')
    assert painted(a, "@Override", hl.BUILTIN) and painted(a, "public", hl.KEYWORD)
    assert b == f"{hl.STRING}body{hl.RESET}"
    assert painted(c, "int", hl.KEYWORD) and painted(c, "3", hl.NUMBER)
    assert not painted(a, "String", hl.KEYWORD)


def test_toml():
    out = hl.highlight('[tool.x]\nname = "w" # c\non = true\nn = 42', "toml")
    assert painted(out, "[tool.x]", hl.KEY) and painted(out, "name", hl.KEY)
    assert painted(out, '"w"', hl.STRING) and painted(out, "# c", hl.COMMENT)
    assert painted(out, "true", hl.BUILTIN) and painted(out, "42", hl.NUMBER)
    assert not painted(out, '"w" # c', hl.STRING)


def test_dockerfile():
    out = hl.highlight('FROM py AS b\n# c\nRUN echo "$HOME" from', "dockerfile")
    assert painted(out, "FROM", hl.KEYWORD) and painted(out, "AS", hl.KEYWORD)
    assert painted(out, "RUN", hl.KEYWORD) and painted(out, "# c", hl.COMMENT)
    assert painted(out, '"$HOME"', hl.STRING)
    assert not painted(out, "from", hl.KEYWORD)


def test_html():
    a, b, c = lines("html", '<a href="x">&amp; <!-- c', "mid", "--> </a>")
    assert painted(a, "<a", hl.KEYWORD) and painted(a, "href", hl.KEY)
    assert painted(a, '"x"', hl.STRING) and painted(a, "&amp;", hl.BUILTIN)
    assert b == f"{hl.COMMENT}mid{hl.RESET}"
    assert painted(c, "</a", hl.KEYWORD)


def test_css():
    out = hl.highlight("@media x { a:hover { color: #fff; margin: 2px !important; } }", "css")
    assert painted(out, "@media", hl.KEYWORD) and painted(out, " color", hl.KEY)
    assert painted(out, "#fff", hl.NUMBER) and painted(out, "2px", hl.NUMBER)
    assert painted(out, "!important", hl.BUILTIN)
    assert not painted(out, " a", hl.KEY) and "a:hover" in out


SAMPLES = {
    "js": "a = `x\ny`; r = /z/g;", "yaml": "k: |\n  v\nj: 1", "cpp": 'R"(a\nb)";',
    "go": "x := `a\nb`", "rust": 'r#"a\nb"#', "java": '"""\nq\n"""', "toml": "a = '''\nb\n'''",
    "dockerfile": "FROM x\nRUN y", "html": "<!--\nc\n--><p>", "css": "/*\nc\n*/ a { b: 1px; }",
}


@pytest.mark.parametrize("lang", sorted(SAMPLES))
def test_strip_roundtrip_and_no_colour(lang, monkeypatch):
    src = SAMPLES[lang]
    assert ANSI.sub("", hl.highlight(src, lang)) == src
    monkeypatch.setattr(ui, "_NO_COLOR", True)
    assert hl.highlight(src, lang) == src


def test_toml_key_rule_is_linear_on_a_long_line_without_equals():
    """2.71.0 verifier: the old key rule took 60 s on 5,000 blanks + ':'."""
    import time
    from wilbur import ui
    from wilbur.highlight import highlight
    saved = ui._NO_COLOR
    ui._NO_COLOR = False
    try:
        start = time.monotonic()
        for line in (" " * 20000 + ":", "a " * 10000 + ":", "k" * 20000):
            highlight(line, "toml")
            highlight(line, "yaml")
        assert time.monotonic() - start < 1.0
        assert "\x1b[" in highlight("my key = 1", "toml").split("=")[0]
        assert "\x1b[" in highlight("- my key: 1", "yaml").split(":")[0]
    finally:
        ui._NO_COLOR = saved
