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
import os
import re
import textwrap
from pathlib import Path

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


# U4 (2.66.0): `path:line` (or `path:line:col`) in prose and inline code
# becomes an OSC 8 file:// link when the path is a real file under the
# project root (or absolute). The visible text is unchanged, so terminals
# without OSC 8 show exactly what the model wrote.
_FILE_REF = re.compile(
    r"(?<![\w/.~:-])((?:\.{0,2}/)?(?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z0-9]+)"
    r":(\d+)(?::\d+)?(?![\w:])")
# The Repl points this at its project root (it can differ from the process
# cwd after /cd); None means the process cwd.
link_root: str | None = None


def _file_url(path: str) -> str | None:
    candidate = Path(path) if os.path.isabs(path) else Path(link_root or os.getcwd()) / path
    try:
        resolved = candidate.resolve()
        return resolved.as_uri() if resolved.is_file() else None
    except (OSError, ValueError):
        return None


def link_files(text: str) -> str:
    """Wrap every existing `path:line` in an OSC 8 file:// hyperlink."""
    if ":" not in text:
        return text

    def sub(m: re.Match) -> str:
        url = _file_url(m.group(1))
        if url is None:
            return m.group(0)
        return f"\033]8;;{url}\033\\{m.group(0)}\033]8;;\033\\"
    return _FILE_REF.sub(sub, text)


def _colour_on() -> bool:
    return not ui._NO_COLOR


def _wrap(text: str, *codes: str) -> str:
    return "".join(codes) + text + ui.RESET


def inline(text: str) -> str:
    """Bold, italic, `code`, [links](url) and path:line file links in one line."""
    if not _colour_on():
        return text
    if not any(ch in text for ch in "`*_["):
        return link_files(text)

    def sub(m: re.Match) -> str:
        tok = m.group(0)
        if m.lastgroup == "code":
            return _wrap(link_files(tok[1:-1]), CODE)
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
    out: list[str] = []
    pos = 0
    for m in _INLINE.finditer(text):
        out.append(link_files(text[pos:m.start()]))
        out.append(sub(m))
        pos = m.end()
    out.append(link_files(text[pos:]))
    return "".join(out)


def _wrapped(body: str, first: str, rest: str, style=None) -> str:
    """U4: word-wrap one prose line at ui.width() with a hanging indent.

    `first` (may hold colour codes) starts line one; `rest` (plain spaces)
    starts every later line. Inline markup is never split across lines: its
    spaces are protected while wrapping. A word longer than the width stays
    whole. `style` renders each piece (default: inline)."""
    render_piece = style or inline
    width = ui.width()
    lead = ui.visible_width(first)
    if lead + len(body) <= width:
        return first + render_piece(body)
    protected = _INLINE.sub(lambda m: m.group(0).replace(" ", "\x00"), body)
    pieces = textwrap.wrap(protected, width=max(10, width - lead), break_long_words=False,
                           break_on_hyphens=False) or [""]
    lines = []
    for index, piece in enumerate(pieces):
        lines.append((first if index == 0 else rest) + render_piece(piece.replace("\x00", " ")))
    return "\n".join(lines)


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
                # A fence only closes on a line of the SAME character with at
                # least as many of them: a 4-backtick fence must not close on
                # a 3-backtick line (that line stays inside the block).
                if (stripped.startswith(self._fence)
                        and stripped.strip(self._fence[0]) == ""
                        and len(stripped) >= len(self._fence)):
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
            indent = m.group(1)
            return _wrapped(m.group(2), indent + ui.c("•", ui.ACCENT) + " ", indent + "  ")
        m = _NUMBERED.match(line)
        if m:
            indent, num = m.group(1), m.group(2)
            return _wrapped(m.group(3), indent + ui.c(num, ui.ACCENT) + " ",
                            indent + " " * (len(num) + 1))
        if line.startswith(">"):
            bar = ui.c("▎", ui.GREY)
            return _wrapped(line[1:].lstrip(), bar, bar,
                            style=lambda piece: ui.c(inline(piece), ITALIC))
        indent = line[:len(line) - len(line.lstrip(" "))]
        return _wrapped(line[len(indent):], indent, indent)


class LiveReply:
    """U1a: one round's reply shown as it streams, for a live display.

    Leading whitespace is dropped (the finished-reply path strips it too).
    With colour on, only complete lines are rendered, through MarkdownStream,
    so a construct split mid-stream (a `**bold**` run, a fence marker, a table
    row) is drawn once, whole, and the concatenation of every `feed` plus
    `close` equals `markdown(text)` for any split of `text`. With colour off
    the pieces pass through raw, as they arrive.
    """

    def __init__(self) -> None:
        self._stream = MarkdownStream()
        self.started = False
        self.text = ""          # the raw reply seen so far, for the caller

    def feed(self, chunk: str) -> str:
        self.text += chunk
        if not self.started:
            chunk = chunk.lstrip()
            if not chunk:
                return ""
            self.started = True
        return self._stream.feed(chunk)

    def close(self) -> str:
        """The rest of the reply, ending in a newline when anything was shown."""
        if not self.started:
            return ""
        if not _colour_on():
            return "" if self.text.endswith("\n") else "\n"
        tail = self._stream.flush()
        return tail + "\n" if tail else ""


def markdown(text: str) -> str:
    """Render a whole reply at once (the same path a stream takes)."""
    if not _colour_on():
        return text
    stream = MarkdownStream()
    head = stream.feed(text)
    tail = stream.flush()
    return (head + tail).rstrip("\n")


# ---------------------------------------------------------------- diffs

# U8 (2.65.0): diff fast path. SequenceMatcher with autojunk=False is quadratic
# on lines that repeat ("", "}", "end"): a 2,000-line rewrite of brace-heavy code
# took 48 s on 2.64.0 before the result rendered. Lines are interned to ints,
# the common head and tail are trimmed first, the exact matcher runs only while
# the changed middle is small, larger middles use autojunk (popular lines junked,
# still a valid edit script), and past DIFF_INPUT_CAP lines the middle is one
# replace. Budget: a 2,000-line rewrite renders in under 10 ms.
DIFF_EXACT_CAP = 250_000      # len(old middle) * len(new middle)
DIFF_INPUT_CAP = 20_000       # lines per side before the middle is one replace


def _diff_opcodes(a: list[str], b: list[str]) -> list[tuple[str, int, int, int, int]]:
    ids: dict[str, int] = {}
    ia = [ids.setdefault(line, len(ids)) for line in a]
    ib = [ids.setdefault(line, len(ids)) for line in b]
    head = 0
    limit = min(len(ia), len(ib))
    while head < limit and ia[head] == ib[head]:
        head += 1
    tail = 0
    while tail < limit - head and ia[-1 - tail] == ib[-1 - tail]:
        tail += 1
    ma, mb = ia[head:len(ia) - tail], ib[head:len(ib) - tail]
    if not ma and not mb:
        middle = []
    elif not ma or not mb or max(len(ma), len(mb)) > DIFF_INPUT_CAP:
        op = "insert" if not ma else "delete" if not mb else "replace"
        middle = [(op, 0, len(ma), 0, len(mb))]
    else:
        exact = len(ma) * len(mb) <= DIFF_EXACT_CAP
        middle = difflib.SequenceMatcher(None, ma, mb, autojunk=not exact).get_opcodes()
    ops: list[tuple[str, int, int, int, int]] = []
    if head:
        ops.append(("equal", 0, head, 0, head))
    for op, i1, i2, j1, j2 in middle:
        ops.append((op, i1 + head, i2 + head, j1 + head, j2 + head))
    if tail:
        ops.append(("equal", len(ia) - tail, len(ia), len(ib) - tail, len(ib)))
    return ops or [("equal", 0, 0, 0, 0)]


def _grouped(ops: list[tuple[str, int, int, int, int]], context: int):
    matcher = difflib.SequenceMatcher(None)
    matcher.opcodes = ops     # get_grouped_opcodes groups whatever get_opcodes returns
    return matcher.get_grouped_opcodes(context)


def diff_counts(old: str, new: str, ops=None) -> tuple[int, int]:
    added = removed = 0
    if ops is None:
        ops = _diff_opcodes(old.splitlines(), new.splitlines())
    for op, i1, i2, j1, j2 in ops:
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
               lang: str | None = None, max_lines: int = 40, ops=None) -> list[str]:
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

    groups = _grouped(ops if ops is not None else _diff_opcodes(a, b), context)
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
    ops = _diff_opcodes(old.splitlines(), new.splitlines())
    added, removed = diff_counts(old, new, ops)
    verb = "Created" if created else "Updated"
    head = (ui.c("  " + ui.BRANCH + "  ", ui.GREY) + f"{verb} {ui.c(path, ui.BOLD)} with "
            + diff_summary(added, removed))
    body = diff_lines(old, new, start=start, max_lines=max_lines, ops=ops)
    return "\n".join([head] + ["     " + r for r in body])


# ---------------------------------------------------------- tool views

_NUMBERED_VIEW = re.compile(r"^(\s*\d+\t)(.*)$")


def file_view_lines(lines: list[str], path: str) -> list[str]:
    """Highlight read_file's `   N\\tcode` lines by the file's language,
    keeping the line-number gutter grey."""
    from .highlight import lang_for_path
    lang = lang_for_path(path)
    if not _colour_on():
        return lines
    hl = Highlighter(lang)
    out = []
    for line in lines:
        m = _NUMBERED_VIEW.match(line)
        if not m:
            out.append(line)
        elif lang is None:
            # Unknown extension: no language to highlight, but the view is
            # still styled -- gutter and body both grey.
            out.append(ui.c(m.group(1), ui.GREY) + ui.c(m.group(2), ui.GREY))
        else:
            out.append(ui.c(m.group(1), ui.GREY) + hl.line(m.group(2)))
    return out


THINKING_MARK = "∴"


def thinking_block(thinking: str, expanded: bool = False, hint: str = "") -> str:
    """U5: the reply's reasoning as a dim block. Collapsed it is one line
    ("∴ Thinking... (N lines, ctrl-r or /thinking)"); expanded it is the
    whole text, indented under the same header. Empty reasoning -> ""."""
    from . import ui
    text = (thinking or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    if not expanded:
        detail = f"{len(lines)} line{'s' if len(lines) != 1 else ''}"
        if hint:
            detail += f", {hint}"
        return ui.c(f"{THINKING_MARK} Thinking... ({detail})", ui.DIM)
    room = max(ui.width() - 4, 20)
    out = [ui.c(f"{THINKING_MARK} Thinking", ui.DIM)]
    import textwrap
    for line in lines:
        for piece in textwrap.wrap(line, room) or [""]:
            out.append(ui.c("  " + piece, ui.DIM))
    return "\n".join(out)
