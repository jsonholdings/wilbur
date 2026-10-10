"""Run horizon and stuck detection: how long a long run may go, and when to
stop it because it is spinning rather than progressing.

Why this exists (owner, 2026-10-02): "if I tell him to work all night on
something, he should do it." The old keep-going loop stopped after 10
continues or 30 minutes -- a hard numeric cap that ended a real all-night task
early. The horizon replaces those caps:

- the default is to run until the task's todo plan is complete,
- a horizon the user states in plain English ("all night", "for 3 hours",
  "until done") is parsed and honoured,
- a configurable safety maximum (default 12h) bounds even "all night",

and a stuck detector -- not a turn count -- is the safety net: it stops when
several turns in a row make no meaningful progress (no file changes, no new
verification, the same error or the same tool call repeated).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# A night of work, capped by the safety maximum. "all night" is a duration the
# owner means literally; 12h is long enough to cover it and short enough that a
# wedged run is not left burning the GPU until noon.
NIGHT_MINUTES = 12 * 60


@dataclass
class Horizon:
    """How long a run may continue. `deadline_minutes` is None for
    until-the-plan-is-done (still bounded by `safety_max_minutes`)."""
    kind: str                      # "until_done" | "duration"
    deadline_minutes: float | None
    safety_max_minutes: float
    phrase: str = ""               # the words that set it, for the log

    @property
    def minutes(self) -> float:
        """The wall-clock budget actually enforced, safety cap included."""
        if self.deadline_minutes is None:
            return self.safety_max_minutes
        return min(self.deadline_minutes, self.safety_max_minutes)


_UNTIL_DONE = re.compile(
    r"\b(until (it'?s? |you'?re? |fully )?(done|finished|complete|completed)|"
    r"until (the )?(task|job|work|everything) (is )?(done|finished|complete)|"
    r"(don'?t|do not) stop (until|till)|keep going until|"
    r"finish (it|the (task|job|work)))\b",
    re.IGNORECASE)

_ALL_NIGHT = re.compile(
    r"\b(all night|overnight|through the night|all day|while i('?m| am) (asleep|away|out)|"
    r"work (on it )?(all|through the) night)\b",
    re.IGNORECASE)

# "for 3 hours", "for 90 minutes", "for an hour", "3h", "45 min", "2 hrs"
_DURATION = re.compile(
    r"\bfor\s+(an?\s+|\d+(?:\.\d+)?\s*)?(hour|hours|hr|hrs|minute|minutes|min|mins|h|m)\b"
    r"|\b(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b",
    re.IGNORECASE)

_HOURS = {"hour", "hours", "hr", "hrs", "h"}


def _to_minutes(qty: float, unit: str) -> float:
    return qty * 60.0 if unit.lower() in _HOURS else qty


def parse_horizon(text: str, safety_max_minutes: float = NIGHT_MINUTES) -> Horizon:
    """Read a horizon out of the user's message. Falls back to until-done."""
    t = text or ""
    if _ALL_NIGHT.search(t):
        m = _ALL_NIGHT.search(t)
        return Horizon("duration", NIGHT_MINUTES, safety_max_minutes, m.group(0))

    m = _DURATION.search(t)
    if m:
        if m.group(2):  # "for <n> <unit>"
            raw = (m.group(1) or "").strip().lower()
            qty = 1.0 if raw in ("", "a", "an") else float(raw)
            minutes = _to_minutes(qty, m.group(2))
        else:           # "<n> <unit>"
            minutes = _to_minutes(float(m.group(3)), m.group(4))
        if minutes > 0:
            return Horizon("duration", minutes, safety_max_minutes, m.group(0).strip())

    if _UNTIL_DONE.search(t):
        m = _UNTIL_DONE.search(t)
        return Horizon("until_done", None, safety_max_minutes, m.group(0))

    return Horizon("until_done", None, safety_max_minutes)


# --------------------------------------------------------------------------- #

@dataclass
class StuckDetector:
    """Stops a run that is spinning. A turn is "progress" when the file-change
    count rose, a new verification was recorded, or the work fingerprint (last
    error + last tool signature) changed from the previous turn. `limit`
    consecutive no-progress turns trips it.
    """
    limit: int = 5
    _last: tuple | None = field(default=None, repr=False)
    stale: int = 0
    tried: list[str] = field(default_factory=list)

    def update(self, *, files_changed: int, verifications: int,
               last_error: str = "", last_tool: str = "") -> bool:
        """Record a turn's outcome; return True when the run is stuck."""
        fingerprint = (files_changed, verifications, last_error, last_tool)
        progressed = (
            self._last is None
            or files_changed > self._last[0]
            or verifications > self._last[1]
            or fingerprint[2:] != self._last[2:]
        )
        if progressed:
            self.stale = 0
        else:
            self.stale += 1
            note = last_error or last_tool or "no change"
            if note not in self.tried:
                self.tried.append(note)
        self._last = fingerprint
        return self.stale >= self.limit

    def report(self) -> str:
        if not self.tried:
            return "no further progress."
        return "no further progress. Tried without effect: " + "; ".join(self.tried[-5:])
