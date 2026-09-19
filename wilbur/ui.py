"""Terminal rendering.

Visual language follows Claude Code: a rounded welcome box, a bullet per tool
call with an indented result branch, dim secondary text, and a single warm
accent colour.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time

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

_NO_COLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()


def c(text: str, *codes: str) -> str:
    if _NO_COLOR:
        return text
    return "".join(codes) + text + RESET


def width() -> int:
    return min(shutil.get_terminal_size((100, 24)).columns, 100)


def banner(model: str, cwd: str, ctx: int, speed: str = "") -> str:
    w = width()
    art = wordmark(w - 2, colour=not _NO_COLOR)
    inner = w - 4
    lines = [
        c(f"{BULLET} Wilbur", ACCENT, BOLD) + c("  local coding agent", GREY),
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


def _ansi_pad(text: str) -> int:
    """Extra ljust width to compensate for non-printing escape sequences."""
    visible, i, n = 0, 0, len(text)
    while i < n:
        if text[i] == "\033":
            while i < n and text[i] != "m":
                i += 1
            i += 1
        else:
            visible += 1
            i += 1
    return len(text) - visible


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else "..." + text[-(limit - 3):]


def tool_call(name: str, summary: str, recovered: bool = False) -> str:
    tag = c(" (recovered)", DIM) if recovered else ""
    return c(BULLET, ACCENT) + " " + c(name, BOLD) + c(f"({summary})", GREY) + tag


def tool_result(output: str, error: bool = False, max_lines: int = 6,
                 seconds: float | None = None) -> str:
    lines = output.strip().splitlines() or ["(no output)"]
    shown = lines[:max_lines]
    colour = RED if error else GREY
    timing = c(f" ({seconds:.1f}s)", DIM) if seconds is not None else ""
    out = [c("  " + BRANCH + "  ", GREY) + c(shown[0][:width() - 6], colour) + timing]
    for line in shown[1:]:
        out.append(c("     ", GREY) + c(line[:width() - 6], colour))
    if len(lines) > max_lines:
        out.append(c(f"     ... +{len(lines) - max_lines} lines", DIM))
    return "\n".join(out)


def assistant(text: str) -> str:
    return text.strip()


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
        self._thread: threading.Thread | None = None
        self._start = 0.0

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

    def _spin(self) -> None:
        i = 0
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(0.05)
                continue
            glyph = SPINNER[i % len(SPINNER)]
            elapsed = int(time.time() - self._start)
            extra = ""
            if self.status_fn is not None:
                try:
                    text = self.status_fn()
                except Exception:
                    text = ""
                if text:
                    extra = f"  {GREY}{text}{RESET}"
            sys.stdout.write(
                f"\r\033[2K{ACCENT}{glyph}{RESET} {GREY}{self.label}... ({elapsed}s){RESET}{extra}"
            )
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.12)
