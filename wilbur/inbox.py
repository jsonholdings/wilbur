"""Bounded message check: a line the user types while Wilbur works is read
within `message_check_max_s`, not only when the whole turn ends.

Cause (traced 2026-10-16): typed lines land in the REPL's `_input_q` and were
read only between whole user turns -- by `_run_loop` and by `_keep_going`
between continuations. Inside `Agent.run` (up to `max_turns` = 60 rounds, each
a model call of up to `request_timeout` = 600 s plus its tool calls), inside a
`/loop` run (up to `loop_max_turns` agent turns, no queue check at all) and
inside a long `run_bash` (e.g. `sleep 3600`) nothing looked at the queue, so a
message could sit unread for hours.

The fix: `Inbox` wraps that queue. The agent asks it between every tool round
and after every tool call (`take_injectable` injects plain lines into the
conversation; a slash command or EOF ends the turn so the REPL handles it),
`run_bash` polls it so a pending message pre-empts a sleep/wait, and the
REPL's watcher thread acknowledges a queued line within the bound and
records the oldest unread one in run.json (`unread_message_since`) so
`wilbur status --check` can fail on one left unread past 2x the bound.
"""
from __future__ import annotations

import queue
import re
import threading
import time
from typing import Callable

DEFAULT_BOUND_S = 60.0

# A command whose whole job is to wait. Only these are pre-empted by a
# pending message; a build or test run is left to finish (killing it would
# lose work), and the message is injected right after it returns.
_WAIT_COMMAND = re.compile(
    # a bare sleep/wait, or an until/while loop whose body is ONLY a sleep
    # (`until [ -f x ]; do sleep 5; done`) and nothing after it.
    r"^\s*(?:timeout\s+\S+\s+)?(?:sleep\s+[\d.]+[smhd]?|wait"
    r"|(?:until|while)\s[^;]*;\s*do\s+sleep\s+[\d.]+[smhd]?\s*;?\s*done)\s*;?\s*$",
    re.S,
)


def is_wait_command(command: str) -> bool:
    return bool(_WAIT_COMMAND.match(command or ""))


def poll_interval(bound_s: float) -> float:
    """How often a background poll looks: well inside the bound."""
    return max(0.05, min(5.0, float(bound_s) / 4.0))


class Inbox:
    """Thread-safe view over the REPL's input queue (items: str, or None = EOF)."""

    def __init__(self, q: "queue.Queue[str | None]",
                 clock: Callable[[], float] = time.time) -> None:
        self._q = q
        self._clock = clock
        self._lock = threading.Lock()
        self._oldest_unread: float | None = None

    def note_arrival(self) -> None:
        """Called when a line is queued: starts the unread clock."""
        with self._lock:
            if self._oldest_unread is None:
                self._oldest_unread = self._clock()

    def pending(self) -> bool:
        """A typed line or slash command is waiting. EOF (None) alone is not a
        message: piped stdin reaches EOF at once, and pausing the turn for it
        would stop every `echo task | wilbur` run after one round."""
        with self._q.mutex:
            return any(isinstance(item, str) for item in self._q.queue)

    def unread_since(self) -> float | None:
        with self._lock:
            if not self.pending():
                self._oldest_unread = None
            return self._oldest_unread

    def take_injectable(self) -> list[str]:
        """Pop plain lines from the FRONT of the queue (stop at a slash
        command or EOF, which the REPL must handle itself)."""
        taken: list[str] = []
        with self._q.mutex:
            dq = self._q.queue
            while dq and isinstance(dq[0], str) and not dq[0].startswith("/"):
                taken.append(dq.popleft())
            remaining = len(dq)
        if taken:
            with self._lock:
                self._oldest_unread = self._clock() if remaining else None
        return taken

    def mark_read(self) -> None:
        with self._lock:
            if not self.pending():
                self._oldest_unread = None


def injection_text(lines: list[str]) -> str:
    joined = "\n".join(lines)
    return ("[New message from the user, sent while you were working. Read it "
            "now and adjust your plan; it outranks the current step.]\n" + joined)


def stale_unread(row: dict, bound_s: float, now: float | None = None) -> float | None:
    """Age in seconds of a run's oldest unread message when it is older than
    2x the bound, else None."""
    since = row.get("unread_message_since")
    if not since:
        return None
    age = (now if now is not None else time.time()) - float(since)
    return age if age > 2 * float(bound_s) else None
