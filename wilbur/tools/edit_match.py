"""Whitespace-tolerant anchor matching for edit_file (2.77.0, edit-robustness).

Local models often copy an anchor with the wrong indentation, trailing spaces,
tabs where the file has spaces, or LF where the file has CRLF. An exact-match
edit then fails and the model retries blindly. This module finds the anchor
line-by-line with leading/trailing whitespace ignored, requires the match to
be unique, re-indents the replacement to the file's indentation, and on a miss
names the closest candidate lines with their line numbers.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass

CANDIDATES_SHOWN = 3
CANDIDATE_LINES_SHOWN = 6
CLOSEST_MIN_RATIO = 0.5
# Scoring every window is O(file lines x anchor lines); past this the miss
# reply falls back to scoring only windows whose first line is similar.
FULL_SCAN_LINE_LIMIT = 4000


def norm(line: str) -> str:
    """Line key used for tolerant comparison: inner runs of whitespace
    collapse and the ends are dropped (indent, trailing spaces, CR, tabs)."""
    return " ".join(line.split())


@dataclass(frozen=True)
class Span:
    start_line: int   # 0-based index of the first matched line
    line_count: int
    start: int        # character offsets into the file text
    end: int


def _lines_with_offsets(text: str) -> tuple[list[str], list[int]]:
    lines = text.splitlines(keepends=True)
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    return lines, offsets


def _anchor_lines(old: str) -> list[str]:
    lines = old.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def find_tolerant(text: str, old: str) -> list[Span]:
    """Every place where `old` matches `text` line-for-line, ignoring
    whitespace at line ends and inside runs. Blank leading/trailing lines of
    the anchor are ignored. Returns [] for an anchor with no visible text."""
    wanted = [norm(line) for line in _anchor_lines(old)]
    if not any(wanted):
        return []
    lines, offsets = _lines_with_offsets(text)
    keys = [norm(line) for line in lines]
    n = len(wanted)
    spans = []
    for s in range(len(lines) - n + 1):
        if keys[s] == wanted[0] and keys[s:s + n] == wanted:
            last = lines[s + n - 1]
            end = offsets[s + n - 1] + len(last.rstrip("\r\n"))
            spans.append(Span(s, n, offsets[s], end))
    return spans


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def reindent(new: str, old: str, matched_lines: list[str], newline: str) -> str | None:
    """Move `new` from the anchor's indentation to the file's, and use the
    file's line ending. Each indent the anchor used maps to the indent of the
    file line it matched (so 4 spaces -> a tab works per level); any other
    indent is shifted by the first line's difference. None when the anchor
    flattened distinct file indents into one, so no mapping is safe."""
    anchor = _anchor_lines(old)
    mapping: dict[str, str] = {}
    for anchor_line, file_line in zip(anchor, matched_lines):
        if anchor_line.strip():
            if mapping.setdefault(_indent(anchor_line), _indent(file_line)) != _indent(file_line):
                return None  # one anchor indent is two file indents: flattened, can't re-indent
    old_indent = _indent(anchor[0]) if anchor else ""
    file_indent = mapping.get(old_indent, "")
    out = []
    for line in new.strip("\r\n").splitlines():
        indent = _indent(line)
        if not line.strip():
            out.append("")
        elif indent in mapping:
            out.append(mapping[indent] + line[len(indent):])
        elif line.startswith(old_indent):
            out.append(file_indent + line[len(old_indent):])
        else:
            out.append(file_indent + line.lstrip(" \t"))
    return newline.join(out)


def closest(text: str, old: str, limit: int = CANDIDATES_SHOWN) -> list[tuple[int, float, list[str]]]:
    """Nearest windows of the file to the anchor: (1-based line, ratio, lines)."""
    anchor = _anchor_lines(old)
    if not anchor:
        return []
    wanted = "\n".join(norm(a) for a in anchor)
    lines = text.splitlines()
    keys = [norm(line) for line in lines]
    n = len(anchor)
    first = norm(anchor[0])
    scored = []
    for s in range(max(1, len(lines) - n + 1)):
        if len(lines) > FULL_SCAN_LINE_LIMIT and \
                difflib.SequenceMatcher(None, keys[s], first).quick_ratio() < CLOSEST_MIN_RATIO:
            continue
        window = "\n".join(keys[s:s + n])
        matcher = difflib.SequenceMatcher(None, window, wanted)
        if matcher.real_quick_ratio() < CLOSEST_MIN_RATIO or matcher.quick_ratio() < CLOSEST_MIN_RATIO:
            continue
        ratio = matcher.ratio()
        if ratio >= CLOSEST_MIN_RATIO:
            scored.append((ratio, s))
    scored.sort(key=lambda item: (-item[0], item[1]))
    picked: list[tuple[int, float, list[str]]] = []
    for ratio, s in scored:
        if any(abs(s - (p[0] - 1)) < n for p in picked):
            continue  # overlapping window of a better candidate
        picked.append((s + 1, ratio, lines[s:s + n]))
        if len(picked) == limit:
            break
    return picked


def format_lines(start_line: int, lines: list[str]) -> str:
    shown = lines[:CANDIDATE_LINES_SHOWN]
    body = "\n".join(f"  {start_line + i:>5}| {line.rstrip()}" for i, line in enumerate(shown))
    if len(lines) > len(shown):
        body += f"\n        ... ({len(lines) - len(shown)} more lines)"
    return body


def closest_reply(text: str, old: str) -> str:
    """The 'closest match' block appended to a not-found error ('' if none)."""
    found = closest(text, old)
    if not found:
        return ""
    parts = ["Closest match" + ("es" if len(found) > 1 else "") + " in the file:"]
    for line_no, ratio, lines in found:
        parts.append(f" lines {line_no}-{line_no + len(lines) - 1} "
                     f"({int(ratio * 100)}% similar):\n{format_lines(line_no, lines)}")
    parts.append("Copy the lines you mean from above (without the line numbers) as old_string.")
    return "\n".join(parts)


def ambiguous_reply(text: str, spans: list[Span]) -> str:
    lines = text.splitlines()
    parts = [f"Matched {len(spans)} places only when whitespace is ignored, so nothing was "
             "edited. Add surrounding lines to make old_string unique:"]
    for span in spans[:5]:
        first = span.start_line + 1
        parts.append(f" lines {first}-{first + span.line_count - 1}:\n"
                     + format_lines(first, lines[span.start_line:span.start_line + span.line_count]))
    return "\n".join(parts)
