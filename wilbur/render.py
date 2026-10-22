"""Display-only rendering of model replies and tool results.

Light markdown (headings, bold/italic, `inline code`, lists, tables, links),
syntax-highlighted fenced code, and Claude Code style coloured diffs for
write_file / edit_file. Nothing here touches what is saved or sent to the
model: callers pass the raw text and print what comes back.

`MarkdownStream` takes the reply in arbitrary chunks, so a fence that opens in
one chunk and closes in a later one still renders as code: only complete lines
are rendered, and the fence/table state lives on the instance.

With colour off (NO_COLOR or a non-TTY stdout) every function returns its
input unchanged, so piped output and logs stay byte-for-byte raw.
"""
from __future__ import annotations

import difflib
import re

from . import ui
from .highlight import Highlighter, normalize_lang

HEAD = "\033[38;5;209m"      # same warm accent as ui.ACCENT
CODE = "\033[38;5;180m"      # inline code: soft tan
LINK = "\033[38;5;110m\033[4m"
ITALIC = "\033[3m"
ADD_FG = "\033[38;5;114m"
DEL_FG = "\033[38;5;203m"
ADD_BG = "\033[48;5;22m"
DEL_BG = "\033[48;5;52m"

_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+[.)])\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_INLINE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<bold>\*\*(?=\S)[^*\n]+?(?<=\S)\*\*|__(?=\S)[^_\n]+?(?<=\S)__)"
    r"|(?P<italic>(?<![\w*])\*(?=\S)[^*\n]+?(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)[^_\n]+?(?<=\S)_(?![\w_]))"
    r"|(?P<link>\[[^\]\n]+\]\([^)\s]+\))"
)


def _colour_on() -> bool:
    return not ui._NO_COLOR


def _wrap(text: str, *codes: str) -> str:
    return "".join(codes) + text + ui.RESET


def inline(text: str) -> str:
    """Bold, italic, `code` and [links](url) inside one line."""
    if not _colour_on() or not any(ch in text for ch in "`*_["):
        return text

    def sub(m: re.Match) -> str:
        tok = m.group(0)
        if m.lastgroup == "code":
            return _wrap(tok[1:-1], CODE)
        if m.lastgroup == "bold":
            return ui.BOLD + inline(tok[2:-2]).replace(ui.RESET, ui.RESET + ui.BOLD) + ui.RESET
        if m.lastgroup == "italic":
            return ITALIC + inline(tok[1:-1]).replace(ui.RESET, ui.RESET + ITALIC) + ui.RESET
        label, url = tok[1:].split("](", 1)
        url = url[:-1]
        # OSC 8 hyperlink: clickable in terminals that support it, invisible
        # (zero width) in those that do not.
        linked = f"\033]8;;{url}\033\\" + _wrap(label, LINK) + "\033]8;;\033\\"
        return linked if label == url else linked + _wrap(f" ({url})", ui.GREY)
    return _INLINE.sub(sub, text)


def _split_cells(row: str) -> list[str]:
    body = row.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


def render_table(rows: list[str]) -> list[str]:
    """Align a markdown table by display cells (ui.visible_width), so wide
    characters and inline styling do not push columns out of line."""
    if not _colour_on():
        return rows
    parsed = [_split_cells(r) for r in rows if not _TABLE_SEP.match(r)]
    has_header = len(rows) > 1 and bool(_TABLE_SEP.match(rows[1]))
    cols = max(len(r) for r in parsed)
    cells = [[inline(cell) for cell in r] + [""] * (cols - len(r)) for r in parsed]
    widths = [max(ui.visible_width(r[i]) for r in cells) for i in range(cols)]
    bar = ui.c("│", ui.GREY)
    out = []
    for n, row in enumerate(cells):
        padded = [cell + " " * (widths[i] - ui.visible_width(cell)) for i, cell in enumerate(row)]
        if n == 0 and has_header:
            padded = [_wrap(p, ui.BOLD) for p in padded]
        out.append(bar + " " + f" {bar} ".join(padded) + " " + bar)
        if n == 0 and has_header:
            out.append(ui.c("├" + "┼".join("─" * (w + 2) for w in widths) + "┤", ui.GREY))
    return out


class MarkdownStream:
    """Feed a reply chunk by chunk; get back rendered complete lines."""

    def __init__(self) -> None:
        self._partial = ""
        self._fence: str | None = None      # the fence marker while inside a code block
        self._hl: Highlighter | None = None
        self._table: list[str] = []

    def feed(self, chunk: str) -> str:
        if not _colour_on():
            return chunk
        self._partial += chunk
        if "\n" not in self._partial:
            return ""
        *complete, self._partial = self._partial.split("\n")
        return "".join(line + "\n" for line in self._lines(complete))

    def flush(self) -> str:
        if not _colour_on():
            return ""
        rest, self._partial = self._partial, ""
        out = list(self._lines([rest] if rest else []))
        out.extend(self._end_table())
        return "\n".join(out)

    def _end_table(self) -> list[str]:
        rows, self._table = self._table, []
        return render_table(rows) if rows else []

    def _lines(self, lines: list[str]):
        for line in lines:
            if self._fence is not None:
                stripped = line.strip()
                if stripped.startswith(self._fence) and stripped.strip(self._fence[0]) == "":
                    self._fence, self._hl = None, None
                    yield ui.c(line, ui.DIM)
                else:
                    yield self._hl.line(line)
                continue
            if self._table and not _TABLE_ROW.match(line):
                yield from self._end_table()
            m = _FENCE.match(line)
            if m:
                self._fence = m.group(2)[0] * 3
                self._hl = Highlighter(normalize_lang(m.group(3)) if m.group(3) else None)
                yield ui.c(line, ui.DIM)
            elif _TABLE_ROW.match(line):
                self._table.append(line)
            else:
                yield self._prose(line)

    @staticmethod
    def _prose(line: str) -> str:
        m = _HEADING.match(line)
        if m:
            return _wrap(m.group(2), ui.BOLD, HEAD)
        if _RULE.match(line):
            return ui.c("─" * min(40, ui.width()), ui.GREY)
        m = _BULLET.match(line)
        if m:
            return m.group(1) + ui.c("•", ui.ACCENT) + " " + inline(m.group(2))
        m = _NUMBERED.match(line)
        if m:
            return m.group(1) + ui.c(m.group(2), ui.ACCENT) + " " + inline(m.group(3))
        if line.startswith(">"):
            return ui.c("▎", ui.GREY) + ui.c(inline(line[1:].lstrip()), ITALIC)
        return inline(line)


def markdown(text: str) -> str:
    """Render a whole reply at once (the same path a stream takes)."""
    if not _colour_on():
        return text
    stream = MarkdownStream()
    head = stream.feed(text)
    tail = stream.flush()
    return (head + tail).rstrip("\n")


# ---------------------------------------------------------------- diffs

def diff_counts(old: str, new: str) -> tuple[int, int]:
    added = removed = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old.splitlines(), new.splitlines(), autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            removed += i2 - i1
        if op in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def diff_summary(added: int, removed: int) -> str:
    def plural(n: int, word: str) -> str:
        return f"{n} {word}{'' if n == 1 else 's'}"
    text = f"{plural(added, 'addition')} and {plural(removed, 'removal')}"
    return text + "  " + ui.c(f"+{added}", ADD_FG) + " " + ui.c(f"−{removed}", DEL_FG)


def diff_lines(old: str, new: str, start: int = 1, context: int = 2,
               lang: str | None = None, max_lines: int = 40) -> list[str]:
    """Numbered, coloured diff rows: removed lines red with the OLD line
    number, added lines green with the NEW one, `context` unchanged lines
    around each change, and `...` between distant hunks."""
    a, b = old.splitlines(), new.splitlines()
    rows: list[str] = []
    num_w = len(str(start + max(len(a), len(b))))
    colour = _colour_on()

    def row(num: int, sign: str, text: str) -> str:
        if not colour:
            return f"{num:>{num_w}} {sign} {text}"
        body = text
        if sign == " ":
            return ui.c(f"{num:>{num_w}}   ", ui.GREY) + ui.c(body, ui.GREY)
        bg, fg = (ADD_BG, ADD_FG) if sign == "+" else (DEL_BG, DEL_FG)
        return _wrap(f"{num:>{num_w}} ", ui.GREY) + _wrap(f"{sign} {body}", bg, fg)

    groups = difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(context)
    for g_index, group in enumerate(groups):
        if g_index:
            rows.append(ui.c(" " * num_w + "  ...", ui.DIM))
        for op, i1, i2, j1, j2 in group:
            if op == "equal":
                rows.extend(row(start + j1 + k, " ", b[j1 + k]) for k in range(i2 - i1))
                continue
            rows.extend(row(start + i, "-", a[i]) for i in range(i1, i2))
            rows.extend(row(start + j, "+", b[j]) for j in range(j1, j2))
    if len(rows) > max_lines:
        hidden = len(rows) - max_lines
        rows = rows[:max_lines] + [ui.c(f"... +{hidden} more diff lines", ui.DIM)]
    return rows


def diff_view(path: str, old: str, new: str, start: int = 1, created: bool = False,
              max_lines: int = 40) -> str:
    """The tool-result block for a write or edit:

        ⎿  Updated wilbur/ui.py with 2 additions and 1 removal  +2 −1
            12   context
            13 - old line
            13 + new line
    """
    added, removed = diff_counts(old, new)
    verb = "Created" if created else "Updated"
    head = (ui.c("  " + ui.BRANCH + "  ", ui.GREY) + f"{verb} {ui.c(path, ui.BOLD)} with "
            + diff_summary(added, removed))
    body = diff_lines(old, new, start=start, max_lines=max_lines)
    return "\n".join([head] + ["     " + r for r in body])


# ---------------------------------------------------------- tool views

_NUMBERED_VIEW = re.compile(r"^(\s*\d+\t)(.*)$")


def file_view_lines(lines: list[str], path: str) -> list[str]:
    """Highlight read_file's `   N\\tcode` lines by the file's language,
    keeping the line-number gutter grey."""
    from .highlight import lang_for_path
    lang = lang_for_path(path)
    if not _colour_on() or lang is None:
        return lines
    hl = Highlighter(lang)
    out = []
    for line in lines:
        m = _NUMBERED_VIEW.match(line)
        out.append(ui.c(m.group(1), ui.GREY) + hl.line(m.group(2)) if m else line)
    return out
