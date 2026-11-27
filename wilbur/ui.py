"""Terminal rendering.

Visual language follows Claude Code: a rounded welcome box, a bullet per tool
call with an indented result branch, dim secondary text, and a single warm
accent colour.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
import unicodedata

from .art import wordmark

ACCENT = "\033[38;5;209m"    # warm terracotta
DIM = "\033[2m"
GREY = "\033[38;5;245m"
BOLD = "\033[1m"
GREEN = "\033[38;5;114m"
RED = "\033[38;5;203m"
BLUE = "\033[38;5;110m"
RESET = "\033[0m"

BULLET = "⏺"            # round tool-call marker
BRANCH = "⎿"            # result continuation
SPINNER = "✻✽✹✽"
# The welcome box needs a marker whose cell width every terminal agrees on.
# BULLET (U+23FA) is an emoji in some terminals (COSMIC draws it as a 2-cell
# orange circle) and text in others, so a box padded for either width breaks
# on the other (owner screenshot 2026-10-21 23:17: the right border jutted out).
BOX_MARK = "●"

_NO_COLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()


def c(text: str, *codes: str) -> str:
    if _NO_COLOR:
        return text
    return "".join(codes) + text + RESET


def width() -> int:
    return min(shutil.get_terminal_size((100, 24)).columns, 100)


def banner(model: str, cwd: str, ctx: int, speed: str = "") -> str:
    # The running version goes in the box on purpose. Wilbur is installed
    # editable, so the code is whatever the source tree held when the process
    # STARTED -- a fresh `wilbur --version` can report a newer build than the
    # session you are sitting in. Printing it on load makes the version you are
    # actually running visible without a second command.
    from . import __version__
    w = width()
    art = wordmark(w - 2, colour=not _NO_COLOR)
    inner = w - 4
    lines = [
        c(f"{BOX_MARK} Wilbur", ACCENT, BOLD) + c(f" v{__version__}", GREY)
        + c("  local coding agent", GREY),
        "",
        c("model  ", GREY) + model + (c(f"  ({speed})", GREY) if speed else ""),
        c("context", GREY) + f"  {ctx:,} tokens",
        c("cwd    ", GREY) + _shorten(cwd, inner - 9),
    ]
    top = c("╭" + "─" * (w - 2) + "╮", ACCENT)
    bot = c("╰" + "─" * (w - 2) + "╯", ACCENT)
    body = "\n".join(
        c("│", ACCENT) + " " + line.ljust(inner + _ansi_pad(line)) + " " + c("│", ACCENT)
        for line in lines
    )
    hint = c("  /help for commands  ·  /model to switch models  ·  ctrl-c to exit", GREY)
    art_block = "\n".join("  " + line for line in art.splitlines())
    return f"\n{art_block}\n\n{top}\n{body}\n{bot}\n{hint}\n"


def _cell_width(ch: str) -> int:
    """Terminal cells one character occupies: 0 for combining and format
    marks, 2 for East Asian wide or full-width (CJK, most emoji), else 1."""
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_width(text: str) -> int:
    """Display width of `text` in terminal cells, ignoring ANSI escapes."""
    cells, i, n = 0, 0, len(text)
    while i < n:
        if text.startswith("\033]", i):
            # OSC (e.g. an OSC 8 hyperlink): runs to ST (ESC \) or BEL and
            # takes no cells; skipping to the next "m" would eat the URL text.
            st, bel = text.find("\033\\", i + 2), text.find("\a", i + 2)
            ends = [e + (2 if e == st else 1) for e in (st, bel) if e != -1]
            i = min(ends) if ends else n
        elif text[i] == "\033":
            while i < n and text[i] != "m":
                i += 1
            i += 1
        else:
            cells += _cell_width(text[i])
            i += 1
    return cells


def _ansi_pad(text: str) -> int:
    """Extra ljust width so `text` fills its cells: ljust counts code points,
    so subtract the cells the text really takes (escapes count 0, wide
    characters 2, combining marks 0)."""
    return len(text) - visible_width(text)


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else "..." + text[-(limit - 3):]


# U2 (2.65.0): Claude-Code-style headers, `Read(path)` rather than `read_file(path)`.
# A tool without a label (MCP, a plugin) keeps its own name.
TOOL_LABELS = {"read_file": "Read", "write_file": "Write", "edit_file": "Update",
               "list_files": "List", "glob": "Glob", "grep": "Search", "run_bash": "Bash",
               "run_tests": "Test", "todo_write": "Update Todos", "task": "Task",
               "generate_image": "Image", "list_skills": "Skills", "load_skill": "Skill"}
STRIKE = "\033[9m"
_NUMBERED_LINE = re.compile(r"^\s*\d+\t")


def tool_call(name: str, summary: str, recovered: bool = False) -> str:
    tag = c(" (recovered)", DIM) if recovered else ""
    label = TOOL_LABELS.get(name, name)
    args = c(f"({summary})", GREY) if summary else ""
    return c(BULLET, ACCENT) + " " + c(label, BOLD) + args + tag


def read_summary(output: str, seconds: float | None = None) -> str | None:
    """`Read 120 lines` for a read_file result, in place of the file's text.
    None when the output is not a numbered view (empty file, past the end)."""
    count = sum(1 for line in output.splitlines() if _NUMBERED_LINE.match(line))
    if not count:
        return None
    timing = c(f" ({seconds:.1f}s)", DIM) if seconds is not None else ""
    word = "line" if count == 1 else "lines"
    return c("  " + BRANCH + "  ", GREY) + "Read " + c(str(count), BOLD) + f" {word}" + timing


def todo_checklist(todos: list[dict[str, str]]) -> str:
    """The plan as a checklist: ☐ pending, bold ☐ in progress, ☒ struck through
    when completed. Plain marks survive NO_COLOR, so the state still reads."""
    rows = []
    for i, todo in enumerate(todos):
        status, task = todo.get("status", "pending"), todo.get("task", "")
        if status == "completed":
            text = c("☒ " + task, GREY, STRIKE)
        elif status == "in_progress":
            text = c("☐ " + task, BOLD)
        else:
            text = "☐ " + task
        lead = "  " + BRANCH + "  " if i == 0 else "     "
        rows.append(c(lead, GREY) + text)
    return "\n".join(rows)


def tool_result(output: str, error: bool = False, max_lines: int = 6,
                 seconds: float | None = None, path: str | None = None,
                 expand_hint: bool = False) -> str:
    """`path` set (a read_file view) highlights the shown lines by language."""
    lines = output.strip("\n").splitlines() or ["(no output)"]
    if not path:
        lines = output.strip().splitlines() or ["(no output)"]
    shown = [line[:width() - 6] for line in lines[:max_lines]]
    colour = RED if error else GREY
    if path and not error:
        from .render import file_view_lines
        styled = file_view_lines(shown, path)
    else:
        styled = [c(line, colour) for line in shown]
    timing = c(f" ({seconds:.1f}s)", DIM) if seconds is not None else ""
    out = [c("  " + BRANCH + "  ", GREY) + styled[0] + timing]
    for line in styled[1:]:
        out.append(c("     ", GREY) + line)
    if len(lines) > max_lines:
        hint = f" ({action_hint('expand_result')})" if expand_hint else ""
        out.append(c(f"     ... +{len(lines) - max_lines} lines{hint}", DIM))
    return "\n".join(out)


def is_cut(output: str, max_lines: int = 6, path: str | None = None) -> bool:
    """True when tool_result() would hide lines of this output."""
    text = output.strip("\n") if path else output.strip()
    return len(text.splitlines()) > max_lines


# U3/U5: key actions, declared once and backend-neutral. The ptk pump maps
# each key to its action today; D3's input backend maps the same table.
# action name -> (key in prompt_toolkit notation, slash command, help text)
KEY_ACTIONS: dict[str, tuple[str, str, str]] = {
    "expand_result": ("c-o", "/expand", "show the last cut tool result in full"),
    "show_thinking": ("c-r", "/thinking", "show the last reply's reasoning"),
}


def key_label(key: str) -> str:
    """'c-o' -> 'ctrl-o' for hints and help."""
    return "ctrl-" + key[2:] if key.startswith("c-") else key


def action_hint(action: str) -> str:
    key, command, _ = KEY_ACTIONS[action]
    return f"{key_label(key)} or {command}"


def status_bar(model: str, last_prompt_tokens: int, num_ctx: int, in_tokens: int,
               out_tokens: int, seconds: float, agents: str = "idle") -> str:
    """The pinned bottom line: model, context used (the last prompt's size
    against num_ctx), token totals and the session's generation speed."""
    pct = 100 * last_prompt_tokens / num_ctx if num_ctx else 0.0
    ctx_colour = RED if pct >= 85 else (ACCENT if pct >= 60 else GREY)
    rate = f"{out_tokens / seconds:.0f} tok/s" if seconds else "-- tok/s"
    return (c(f"model: {model}  ·  ", GREY) + c(f"context {pct:.0f}%", ctx_colour)
            + c(f"  ·  tokens: {in_tokens:,} in / {out_tokens:,} out  ·  {rate}"
                f"  ·  {agents}", GREY))


def assistant(text: str) -> str:
    """A finished reply, with light markdown and highlighted code blocks.
    Display only: the session keeps the raw text."""
    from .render import markdown
    return markdown(text.strip())


def signoff(text: str) -> str:
    return c(text, BOLD, ACCENT)


def notice(text: str) -> str:
    return c("  " + text, GREY)


def error(text: str) -> str:
    return c("  " + text, RED)


def success(text: str) -> str:
    return c("  " + text, GREEN)


def prompt() -> str:
    return c("› ", ACCENT)


class Spinner:
    """Claude Code's pulsing glyph with an elapsed-seconds counter.

    Must be paused whenever anything else wants the terminal line -- most
    importantly an approval prompt. Without this the spinner thread keeps
    redrawing over the `allow? [y/N]` line every ~0.12s while `input()` is
    blocking, which hides the prompt and any characters the user types under
    it (2026-09-18 concurrency-session debug logs: every tool call came back
    "User denied" during a run where Ollama never received the requests --
    the prompts were never legible, not actually refused on the merits).
    """

    def __init__(self, label: str = "Thinking", status_fn=None, render: bool = True) -> None:
        self.label = label
        # Called on every redraw to get an extra bit of text after the
        # elapsed-seconds counter -- used to show "N subagents running"
        # without the spinner needing to know what a subagent is.
        self.status_fn = status_fn
        # False when something else (the prompt_toolkit bottom toolbar) owns
        # rendering the status -- the spinner still tracks elapsed time and
        # label for that toolbar to read, it just never writes to stdout
        # itself. A `\r`-redraw loop and a live prompt_toolkit prompt fight
        # over the same terminal line, so exactly one of them may draw.
        self.render = render
        self._stop = threading.Event()
        self._paused = threading.Event()
        # U1b: one writer at a time on the spinner's line, so a streamed reply
        # line and a redraw never interleave mid-escape.
        self._draw = threading.Lock()
        self._thread: threading.Thread | None = None
        self._start = 0.0
        # The model call's live phase ("reading 48,813 tokens", "thinking",
        # "writing") and when it began, so a long prompt read shows its own
        # ticking seconds instead of looking frozen. None between calls.
        self.phase: str | None = None
        self._phase_start = 0.0

    def set_phase(self, text: str | None) -> None:
        self.phase = text
        self._phase_start = time.time()

    def status_text(self) -> str:
        """`Thinking... (40s)`, or with a live model phase
        `reading 48,813 tokens 12s · turn 40s`."""
        elapsed = int(time.time() - self._start) if self._start else 0
        if self.phase:
            return (f"{self.phase} {int(time.time() - self._phase_start)}s"
                    f" · turn {elapsed}s")
        return f"{self.label}... ({elapsed}s)"

    def __enter__(self) -> "Spinner":
        self._start = time.time()
        if _NO_COLOR or not self.render:
            return self
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
        if not _NO_COLOR and self.render:
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()

    def pause(self) -> None:
        """Stop redrawing and clear the line so another prompt owns it.
        Idempotent: clearing again while already paused would erase what the
        user is typing on that line."""
        if self._paused.is_set():
            return
        self._paused.set()
        if not _NO_COLOR:
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()

    def resume(self) -> None:
        self._paused.clear()

    @property
    def held(self) -> bool:
        """True while something else owns the line (an approval prompt, a
        line being typed): a streamed reply must hold back until resume()."""
        return self._paused.is_set()

    def write_above(self, text: str) -> None:
        """U1b: print `text` (complete lines) where the spinner line is; the
        next redraw puts the spinner back under it."""
        with self._draw:
            if self.render and not _NO_COLOR and self._thread is not None:
                sys.stdout.write("\r\033[2K")
            sys.stdout.write(text)
            sys.stdout.flush()

    def _spin(self) -> None:
        i = 0
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(0.05)
                continue
            glyph = SPINNER[i % len(SPINNER)]
            extra = ""
            if self.status_fn is not None:
                try:
                    text = self.status_fn()
                except Exception:
                    text = ""
                if text:
                    extra = f"  {GREY}{text}{RESET}"
            with self._draw:
                if self._paused.is_set():
                    continue
                sys.stdout.write(
                    f"\r\033[2K{ACCENT}{glyph}{RESET} {GREY}{self.status_text()}{RESET}{extra}"
                )
                sys.stdout.flush()
            i += 1
            self._stop.wait(0.12)
