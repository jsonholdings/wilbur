"""A small in-process registry of subagents: in-flight and recently finished.

`Task.run` (this package's `task.py`) registers a record before starting a
subagent's thread, updates it as the subagent's own agent loop reports
events, and marks it finished in every exit path. `/agents` in
`wilbur/repl.py` reads a snapshot to print a table; the spinner reads
`running()` to show a one-line status while any subagent is in flight.

Module-level and lock-guarded rather than attached to one Agent instance:
subagents are launched from deep inside a tool call on whatever thread is
running the parent's turn, and the REPL that wants to display them is a
different object entirely. A single process-wide table is the simplest
thing that lets both sides see the same state without threading a reference
through `ToolContext`.
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field

_lock = threading.Lock()
_id_seq = itertools.count(1)
_records: dict[int, "AgentRecord"] = {}

# Finished records are kept around (capped) so `/agents` can show recent
# history, not just what is running right now.
_MAX_FINISHED = 20


@dataclass
class AgentRecord:
    id: int
    label: str
    kind: str
    started: float = field(default_factory=time.time)
    status: str = "running"     # running | done | error | cancelled | timeout
    round: int = 0
    current: str = ""           # e.g. "tool: read_file"

    @property
    def elapsed(self) -> float:
        return time.time() - self.started


def register(label: str, kind: str) -> AgentRecord:
    rec = AgentRecord(id=next(_id_seq), label=label, kind=kind)
    with _lock:
        _records[rec.id] = rec
    return rec


def update(rec_id: int, **fields) -> None:
    with _lock:
        rec = _records.get(rec_id)
        if rec is None:
            return
        for key, value in fields.items():
            setattr(rec, key, value)


def finish(rec_id: int, status: str = "done") -> None:
    update(rec_id, status=status)
    with _lock:
        finished_ids = [i for i, r in _records.items() if r.status != "running"]
        # Oldest-first, drop the overflow -- a long session should not grow
        # this dict forever.
        excess = len(finished_ids) - _MAX_FINISHED
        if excess > 0:
            for i in sorted(finished_ids,
                             key=lambda i: _records[i].started)[:excess]:
                del _records[i]


def snapshot() -> list[AgentRecord]:
    with _lock:
        return sorted(_records.values(), key=lambda r: r.id)


def running() -> list[AgentRecord]:
    return [r for r in snapshot() if r.status == "running"]


def clear() -> None:
    """Test-only: reset the module-level table between test cases."""
    with _lock:
        _records.clear()
