"""Dependency-free, line-oriented syntax highlighter (stdlib ``re`` only).

One combined regex per language, compiled lazily once. ``Highlighter.line``
carries state across lines for multi-line strings/comments. With
``ui._NO_COLOR`` set (checked at call time) output equals input byte for byte,
and stripping ANSI from coloured output always gives back the input.
"""
from __future__ import annotations

import builtins as _builtins
import keyword as _keyword
import os
import re

from wilbur import ui

KEYWORD = "\033[38;5;176m"
STRING = "\033[38;5;150m"
COMMENT = "\033[38;5;244m"
NUMBER = "\033[38;5;215m"
BUILTIN = "\033[38;5;80m"
KEY = "\033[38;5;110m"
ADDED = "\033[38;5;114m"
REMOVED = "\033[38;5;203m"
HUNK = "\033[38;5;81m"
HEADER = "\033[1m"
HEADING = "\033[1m\033[38;5;209m"
CODE = "\033[38;5;180m"
RESET = "\033[0m"

_ALIASES = {
    "python": "python", "py": "python", "python3": "python",
    "js": "js", "javascript": "js", "ts": "js", "typescript": "js",
    "jsx": "js", "tsx": "js", "mjs": "js",
    "sh": "shell", "bash": "shell", "zsh": "shell", "shell": "shell", "console": "shell",
    "json": "json", "yaml": "yaml", "yml": "yaml",
    "c": "c", "h": "c", "cpp": "c", "c++": "c", "cc": "c", "hpp": "c",
    "csharp": "csharp", "cs": "csharp", "c#": "csharp",
    "php": "php", "sql": "sql", "markdown": "markdown", "md": "markdown",
    "diff": "diff", "patch": "diff",
    "go": "go", "golang": "go", "rs": "rust", "rust": "rust", "java": "java",
    "toml": "toml", "dockerfile": "dockerfile", "docker": "dockerfile",
    "html": "html", "htm": "html", "xml": "html", "css": "css",
}
_EXT = {
    ".py": "python", ".js": "js", ".mjs": "js", ".ts": "js", ".tsx": "js", ".jsx": "js",
    ".sh": "shell", ".bash": "shell", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".c": "c", ".h": "c", ".cpp": "c", ".hpp": "c", ".cc": "c", ".cs": "csharp",
    ".php": "php", ".sql": "sql", ".md": "markdown", ".diff": "diff", ".patch": "diff",
    ".go": "go", ".rs": "rust", ".java": "java", ".toml": "toml", ".dockerfile": "dockerfile",
    ".html": "html", ".htm": "html", ".css": "css",
}


def normalize_lang(tag: str | None) -> str | None:
    if not tag:
        return None
    return _ALIASES.get(tag.strip().lower())


def lang_for_path(path: str) -> str | None:
    base = os.path.basename(path or "")
    if base == "Dockerfile" or base.startswith("Dockerfile.") or base == "Containerfile":
        return "dockerfile"
    return _EXT.get(os.path.splitext(base)[1].lower())


def _words(ws) -> str:
    return r"\b(?:" + "|".join(sorted(map(re.escape, ws), key=len, reverse=True)) + r")\b"


_NUM = r"\b(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?)[a-zA-Z]*\b"
_DQ = r'"(?:[^"\\]|\\.)*"?'
_SQ = r"'(?:[^'\\]|\\.)*'?"

_PY_BUILTINS = [n for n in dir(_builtins) if not n.startswith("_")]
_JS_KW = ("break case catch class const continue debugger default delete do else export extends "
          "finally for function if import in instanceof let new return super switch this throw try "
          "typeof var void while with yield async await of static get set interface type enum "
          "implements private public protected readonly declare namespace abstract as from").split()
_JS_BI = ("true false null undefined NaN Infinity console Object Array String Number Boolean "
          "Promise Math JSON Map Set Symbol Error window document require module").split()
_C_KW = ("auto break case char const continue default do double else enum extern float for goto if "
         "inline int long register restrict return short signed sizeof static struct switch typedef "
         "union unsigned void volatile while bool class namespace template typename public private "
         "protected virtual override new delete this using try catch throw nullptr constexpr "
         "noexcept operator friend explicit mutable static_cast reinterpret_cast const_cast "
         "dynamic_cast true false").split()
_CS_KW = ("abstract as base bool break byte case catch char checked class const continue decimal "
          "default delegate do double else enum event explicit extern false finally fixed float for "
          "foreach goto if implicit in int interface internal is lock long namespace new null object "
          "operator out override params private protected public readonly ref return sbyte sealed "
          "short sizeof stackalloc static string struct switch this throw true try typeof uint ulong "
          "unchecked unsafe ushort using var virtual void volatile while async await get set record "
          "yield dynamic nameof when where").split()
_PHP_KW = ("abstract and array as break callable case catch class clone const continue declare "
           "default do echo else elseif empty enddeclare endfor endforeach endif endswitch endwhile "
           "extends final finally fn for foreach function global goto if implements include "
           "include_once instanceof insteadof interface isset list match namespace new or print "
           "private protected public readonly require require_once return static switch throw trait "
           "try unset use var while xor yield true false null").split()
_SH_KW = ("if then else elif fi case esac for while until do done in function select time return "
          "exit export local readonly declare unset shift break continue").split()
_SH_BI = ("echo printf cd ls cat grep sed awk test read source eval exec set trap pwd mkdir rm cp mv "
          "chmod chown find xargs sudo git curl").split()
_SQL_KW = ("select from where and or not insert into values update set delete create table drop alter "
           "index view join inner left right outer full on as group by order having limit offset "
           "distinct union all exists in is null like between case when then else end primary key "
           "foreign references default unique begin commit rollback transaction with returning "
           "asc desc count sum avg min max").split()

_GO_KW = ("break case chan const continue default defer else fallthrough for func go goto if "
          "import interface map package range return select struct switch type var").split()
_GO_BI = ("true false nil iota append cap close copy delete len make new panic print println "
          "recover error string int int8 int16 int32 int64 uint uint8 uint16 uint32 uint64 uintptr "
          "float32 float64 byte rune bool any complex64 complex128").split()
_RS_KW = ("as async await break const continue crate dyn else enum extern false fn for if impl in "
          "let loop match mod move mut pub ref return self Self static struct super trait true type "
          "unsafe use where while").split()
_RS_BI = ("Some None Ok Err Box Vec String Option Result i8 i16 i32 i64 i128 isize u8 u16 u32 u64 "
          "u128 usize f32 f64 bool char str").split()
_JAVA_KW = ("abstract assert boolean break byte case catch char class const continue default do "
            "double else enum extends final finally float for goto if implements import instanceof "
            "int interface long native new package private protected public return short static "
            "strictfp super switch synchronized this throw throws transient try void volatile while "
            "var record sealed permits yield true false null").split()
_DOCKER_KW = ("FROM RUN CMD LABEL MAINTAINER EXPOSE ENV ADD COPY ENTRYPOINT VOLUME USER WORKDIR ARG "
              "ONBUILD STOPSIGNAL HEALTHCHECK SHELL").split()
# JS regex literal: only where an operand is expected (after an operator/opener or `return`),
# so `a / b / c` stays division.
_JS_RE_PREV = r"[(,=:\[!&|?{};]"
_JS_REGEX = (r"(?:^\s*|(?<=" + _JS_RE_PREV + r")\s*|(?<=return\s))"
             r"/(?![/*\s])(?:[^/\\\[\n]|\\.|\[(?:[^\]\\]|\\.)*\])+/[dgimsuyv]*")

# (spec list, multi-line openers). Each spec: (group, pattern, colour).
_SPECS: dict[str, list[tuple[str, str, str]]] = {
    "python": [
        ("tq", r"(?:[rRbBuUfF]{0,2})(?:'''|\"\"\")", STRING),
        ("comment", r"#.*", COMMENT),
        ("string", r"(?:[rRbBuUfF]{0,2})(?:" + _DQ + "|" + _SQ + ")", STRING),
        ("keyword", _words(_keyword.kwlist + list(getattr(_keyword, "softkwlist", []))), KEYWORD),
        ("builtin", _words(_PY_BUILTINS), BUILTIN),
        ("number", _NUM, NUMBER),
    ],
    "js": [
        ("bc", r"/\*", COMMENT),
        ("comment", r"//.*", COMMENT),
        ("tl", r"`", STRING),
        ("regex", _JS_REGEX, STRING),
        ("string", _DQ + "|" + _SQ, STRING),
        ("keyword", _words(_JS_KW), KEYWORD),
        ("builtin", _words(_JS_BI), BUILTIN),
        ("number", _NUM, NUMBER),
    ],
    "c": [
        ("bc", r"/\*", COMMENT),
        ("comment", r"//.*|^\s*#\s*\w+", COMMENT),
        ("raw", r'(?:u8|[uUL])?R"[^()\\\s"]{0,16}\(', STRING),
        ("string", _DQ + "|" + _SQ, STRING),
        ("keyword", _words(_C_KW), KEYWORD),
        ("number", _NUM, NUMBER),
    ],
    "csharp": [
        ("bc", r"/\*", COMMENT),
        ("comment", r"//.*|^\s*#\s*\w+", COMMENT),
        ("string", r'@"(?:[^"]|"")*"?|\$?' + _DQ + "|" + _SQ, STRING),
        ("keyword", _words(_CS_KW), KEYWORD),
        ("number", _NUM, NUMBER),
    ],
    "php": [
        ("bc", r"/\*", COMMENT),
        ("comment", r"(?://|#).*", COMMENT),
        ("string", _DQ + "|" + _SQ, STRING),
        ("keyword", _words(_PHP_KW), KEYWORD),
        ("builtin", r"\$\w+|<\?php|\?>", BUILTIN),
        ("number", _NUM, NUMBER),
    ],
    "shell": [
        ("comment", r"(?:^|(?<=\s))#.*", COMMENT),
        ("string", _DQ + "|'[^']*'?", STRING),
        ("keyword", _words(_SH_KW), KEYWORD),
        ("builtin", _words(_SH_BI) + r"|\$\{?\w+\}?", BUILTIN),
        ("number", r"\b\d+\b", NUMBER),
    ],
    "sql": [
        ("bc", r"/\*", COMMENT),
        ("comment", r"--.*", COMMENT),
        ("string", _SQ + "|" + _DQ, STRING),
        ("keyword", _words(_SQL_KW), KEYWORD),
        ("number", _NUM, NUMBER),
    ],
    "json": [
        ("key", _DQ + r"(?=\s*:)", KEY),
        ("string", _DQ, STRING),
        ("builtin", r"\b(?:true|false|null)\b", BUILTIN),
        ("number", r"-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b", NUMBER),
    ],
    "yaml": [
        ("comment", r"(?:^|(?<=\s))#.*", COMMENT),
        ("blk", r"(?:(?<=:)|(?<=^-)|(?<=\s-))[ \t]+[|>][+-]?[1-9]?[+-]?[ \t]*(?:#.*)?$", STRING),
        # Same linear shape as the TOML key (the lazy class with a space was
        # quadratic on a long blank line: 16,000 blanks took 1.9 s).
        ("key", r"^[ \t]*(?:-[ \t]+)?[\w.\-\"']+(?:[ \t]+[\w.\-\"']+)*(?=:(?:\s|$))", KEY),
        ("string", _DQ + "|" + _SQ, STRING),
        ("builtin", r"\b(?:true|false|null|yes|no|on|off)\b|^---$|^\.\.\.$", BUILTIN),
        ("number", r"(?<![\w.])-?\d+(?:\.\d+)?\b", NUMBER),
    ],
    "go": [
        ("bc", r"/\*", COMMENT),
        ("comment", r"//.*", COMMENT),
        ("rawbt", r"`", STRING),
        ("string", _DQ + "|" + _SQ, STRING),
        ("keyword", _words(_GO_KW), KEYWORD),
        ("builtin", _words(_GO_BI), BUILTIN),
        ("number", _NUM, NUMBER),
    ],
    "rust": [
        ("bc", r"/\*", COMMENT),
        ("comment", r"//.*", COMMENT),
        ("rsraw", r'\bb?r#*"', STRING),
        ("string", r"b?" + _DQ + r"|b?'(?:[^'\\\n]|\\[^'\n]+)'", STRING),
        ("keyword", _words(_RS_KW), KEYWORD),
        ("builtin", _words(_RS_BI) + r"|\b\w+!", BUILTIN),
        ("number", _NUM, NUMBER),
    ],
    "java": [
        ("bc", r"/\*", COMMENT),
        ("comment", r"//.*", COMMENT),
        ("tq", r'"""', STRING),
        ("string", _DQ + "|" + _SQ, STRING),
        ("keyword", _words(_JAVA_KW), KEYWORD),
        ("builtin", r"@\w+", BUILTIN),
        ("number", _NUM, NUMBER),
    ],
    "toml": [
        ("comment", r"#.*", COMMENT),
        # A key is words split by mandatory blanks (no shared characters), so a long
        # line with no "=" fails in linear time; a lazy class that also held the
        # space backtracked cubically (5,000 blanks took 60 s, 2.71.0 verifier).
        ("key", r"^[ \t]*\[\[?[^\]\n]*\]\]?|^[ \t]*[\w.\-\"']+(?:[ \t]+[\w.\-\"']+)*(?=[ \t]*=)", KEY),
        ("tq", r'"""' + "|" + "'" * 3, STRING),
        ("string", _DQ + "|'[^'\n]*'?", STRING),
        ("builtin", r"\b(?:true|false)\b", BUILTIN),
        ("number", r"(?<![\w.])[+-]?\d[\d_:.TZ+-]*\b", NUMBER),
    ],
    "dockerfile": [
        ("comment", r"^\s*#.*", COMMENT),
        ("keyword", r"(?i:^\s*(?:" + "|".join(_DOCKER_KW) + r")\b|(?<=\s)AS\b)", KEYWORD),
        ("string", _DQ + "|" + _SQ, STRING),
        ("builtin", r"\$\{?\w+\}?", BUILTIN),
    ],
    "html": [
        ("hc", r"<!--", COMMENT),
        ("keyword", r"</?[A-Za-z][\w:-]*|/?>|<!DOCTYPE\b", KEYWORD),
        ("key", r"(?<=\s)[A-Za-z_:][\w:.-]*(?=\s*=)", KEY),
        ("string", _DQ + "|" + _SQ, STRING),
        ("builtin", r"&(?:#\d+|#x[0-9a-fA-F]+|\w+);", BUILTIN),
    ],
    "css": [
        ("bc", r"/\*", COMMENT),
        ("string", _DQ + "|" + _SQ, STRING),
        ("keyword", r"@[\w-]+", KEYWORD),
        ("key", r"(?:^|(?<=[{;]))\s*-{0,2}[A-Za-z][\w-]*(?=\s*:[^{]*(?:;|}|$))", KEY),
        ("builtin", r"!important\b", BUILTIN),
        ("number", r"#[0-9a-fA-F]{3,8}\b|(?<![\w-])-?\d+(?:\.\d+)?(?:px|em|rem|vh|vw|ms|s|deg|fr|%)?", NUMBER),
    ],
    "markdown": [
        ("code", r"`[^`]*`", CODE),
    ],
}
_ML = ("bc", "tq", "tl", "raw", "hc", "rawbt", "rsraw", "blk")
_COMPILED: dict[str, re.Pattern] = {}
_COLOUR: dict[str, dict[str, str]] = {}


def _regex(lang: str) -> re.Pattern:
    rx = _COMPILED.get(lang)
    if rx is None:
        specs = _SPECS[lang]
        flags = re.IGNORECASE if lang == "sql" else 0
        rx = re.compile("|".join(f"(?P<{g}>{p})" for g, p, _ in specs), flags)
        _COMPILED[lang] = rx
        _COLOUR[lang] = {g: col for g, _, col in specs}
    return rx


def _paint(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if text else ""


class Highlighter:
    def __init__(self, lang: str | None):
        self.lang = normalize_lang(lang)
        # Open multi-line construct: (closer, backslash-escapes?, colour), or
        # ("", False, indent) for a YAML block scalar (ends at a line indented <= indent).
        self._state: tuple | None = None

    def line(self, text: str) -> str:
        if ui._NO_COLOR or self.lang is None:
            self._track_plain(text)
            return text
        if self.lang == "diff":
            return self._diff(text)
        if self.lang == "markdown":
            m = re.match(r"\s{0,3}#{1,6}(?:\s|$)", text)
            if m:
                return _paint(text, HEADING)
            if text.lstrip().startswith("```"):
                return _paint(text, CODE)
        return self._scan(text, emit=True)

    def _track_plain(self, text: str) -> None:
        if self.lang in _SPECS and any(g in _ML for g, _, _ in _SPECS[self.lang]):
            self._scan(text, emit=False)

    @staticmethod
    def _diff(text: str) -> str:
        if text.startswith(("+++", "---", "diff ", "index ")):
            return _paint(text, HEADER)
        if text.startswith("@@"):
            return _paint(text, HUNK)
        if text.startswith("+"):
            return _paint(text, ADDED)
        if text.startswith("-"):
            return _paint(text, REMOVED)
        return text

    @staticmethod
    def _opener(g: str, tok: str, text: str) -> tuple:
        if g == "bc":
            return ("*/", False, COMMENT)
        if g == "hc":
            return ("-->", False, COMMENT)
        if g == "tq":
            return (tok[-3:], True, STRING)
        if g == "tl":
            return ("`", True, STRING)
        if g == "rawbt":
            return ("`", False, STRING)
        if g == "raw":   # C++ R"delim( ... )delim"
            return (")" + tok[tok.index('"') + 1:-1] + '"', False, STRING)
        if g == "rsraw":  # Rust r#"..."#
            return ('"' + "#" * tok.count("#"), False, STRING)
        return ("", False, len(text) - len(text.lstrip()))   # YAML block scalar

    def _close(self, text: str, pos: int) -> int:
        """Return the end index of the open multi-line construct, or -1."""
        closer, escapes, _ = self._state
        i = pos
        while True:
            j = text.find(closer, i)
            if j < 0:
                return -1
            k = j - 1
            bs = 0
            while escapes and k >= pos and text[k] == "\\":
                bs += 1
                k -= 1
            if bs % 2 == 0:
                return j + len(closer)
            i = j + 1

    def _scan(self, text: str, emit: bool) -> str:
        rx = _regex(self.lang)
        colours = _COLOUR[self.lang]
        out: list[str] = []
        pos = 0
        n = len(text)
        if self._state and self._state[0] == "":
            indent = len(text) - len(text.lstrip())
            if not text.strip() or indent > self._state[2]:
                return _paint(text, STRING) if emit else ""
            self._state = None
        while pos <= n:
            if self._state:
                col = self._state[2]
                end = self._close(text, pos)
                if end < 0:
                    if emit:
                        out.append(_paint(text[pos:], col))
                    return "".join(out)
                if emit:
                    out.append(_paint(text[pos:end], col))
                self._state = None
                pos = end
                continue
            restart = False
            for m in rx.finditer(text, pos):
                g = m.lastgroup
                if emit:
                    out.append(text[pos:m.start()])
                if g in _ML:
                    self._state = self._opener(g, m.group(), text)
                    end = self._close(text, m.end()) if g != "blk" else -1
                    if end >= 0:   # closes on the same line: paint it as one token
                        self._state = None
                    else:
                        end = m.end()
                    if emit:
                        out.append(_paint(text[m.start():end], colours[g]))
                    pos = end
                    restart = g != "blk"
                    break
                if emit:
                    out.append(_paint(m.group(), colours[g]))
                pos = m.end()
            if not restart:
                break
        if emit:
            out.append(text[pos:])
        return "".join(out)


def highlight(code: str, lang: str | None) -> str:
    h = Highlighter(lang)
    parts = re.split(r"(\r\n|\n|\r)", code)
    return "".join(p if i % 2 else h.line(p) for i, p in enumerate(parts))
