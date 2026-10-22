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
}
_EXT = {
    ".py": "python", ".js": "js", ".mjs": "js", ".ts": "js", ".tsx": "js", ".jsx": "js",
    ".sh": "shell", ".bash": "shell", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".c": "c", ".h": "c", ".cpp": "c", ".hpp": "c", ".cc": "c", ".cs": "csharp",
    ".php": "php", ".sql": "sql", ".md": "markdown", ".diff": "diff", ".patch": "diff",
}


def normalize_lang(tag: str | None) -> str | None:
    if not tag:
        return None
    return _ALIASES.get(tag.strip().lower())


def lang_for_path(path: str) -> str | None:
    return _EXT.get(os.path.splitext(os.path.basename(path or ""))[1].lower())


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
        ("string", _DQ + "|" + _SQ + r"|`(?:[^`\\]|\\.)*`?", STRING),
        ("keyword", _words(_JS_KW), KEYWORD),
        ("builtin", _words(_JS_BI), BUILTIN),
        ("number", _NUM, NUMBER),
    ],
    "c": [
        ("bc", r"/\*", COMMENT),
        ("comment", r"//.*|^\s*#\s*\w+", COMMENT),
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
        ("key", r"^\s*(?:-\s+)?[\w.\-\"' ]+?(?=:(?:\s|$))", KEY),
        ("string", _DQ + "|" + _SQ, STRING),
        ("builtin", r"\b(?:true|false|null|yes|no|on|off)\b|^---$|^\.\.\.$", BUILTIN),
        ("number", r"(?<![\w.])-?\d+(?:\.\d+)?\b", NUMBER),
    ],
    "markdown": [
        ("code", r"`[^`]*`", CODE),
    ],
}
_BC_CLOSE = re.compile(r"\*/")
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
        self._state: str | None = None   # "bc" or a python triple-quote delimiter

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
        if self.lang in _SPECS and any(g in ("bc", "tq") for g, _, _ in _SPECS[self.lang]):
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

    def _close(self, text: str, pos: int) -> int:
        """Return the end index of the open multi-line construct, or -1."""
        if self._state == "bc":
            m = _BC_CLOSE.search(text, pos)
            return m.end() if m else -1
        delim = self._state
        i = pos
        while True:
            j = text.find(delim, i)
            if j < 0:
                return -1
            k = j - 1
            bs = 0
            while k >= pos and text[k] == "\\":
                bs += 1
                k -= 1
            if bs % 2 == 0:
                return j + 3
            i = j + 1

    def _scan(self, text: str, emit: bool) -> str:
        rx = _regex(self.lang)
        colours = _COLOUR[self.lang]
        out: list[str] = []
        pos = 0
        n = len(text)
        while pos <= n:
            if self._state:
                col = COMMENT if self._state == "bc" else STRING
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
                if g in ("bc", "tq"):
                    self._state = "bc" if g == "bc" else m.group()[-3:]
                    if emit:
                        out.append(_paint(m.group(), colours[g]))
                    pos = m.end()
                    restart = True
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
