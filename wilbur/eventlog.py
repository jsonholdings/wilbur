"""E3a (2.70.0): the structured event log, one JSON object per line.

For machines (E2's crash-recovery test, later metrics), not for the user: the
terminal display is unchanged, and `progress.md` stays the human log.

Schema, one line per event:
    {"ts": <unix seconds>, "run_id": str, "session_id": str,
     "event": str, "round": int, "data": {...}}

Privacy: `data` keeps numbers and booleans as they are. A string survives only
under a key in SAFE_STR_KEYS (tool name, phase, stop reason...), redacted
through `wilbur.redact` and cut to STR_MAX; any other string (reply text,
reasoning, tool output, tool arguments, call signatures) is replaced by its
length, so model text and file contents never reach the file. A list is kept
as its length. The file is created 0600 in a 0700 directory under the state
dir (beside the sessions dir), rotated at `max_bytes`, and never mirrored
(it is runtime data, not source).
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import config as _config
from .redact import redact_text, secret_values

SAFE_STR_KEYS = frozenset({"name", "phase", "reason", "kind", "label", "error", "mode"})
STR_MAX = 200
# High-rate display events: one per streamed chunk or status tick.
SKIPPED_EVENTS = frozenset({"assistant_delta", "model_phase", "message"})
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_KEEP = 3


def log_path() -> Path:
    # SESSION_DIR is read at call time so WILBUR_SESSIONS and tests that
    # redirect it are honoured; the log sits beside the sessions dir.
    override = os.environ.get("WILBUR_EVENT_LOG", "")
    if override:
        return Path(override)
    return _config.SESSION_DIR.parent / "events" / "events.jsonl"


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def scrub(data: dict[str, Any] | None, values: list[str]) -> dict[str, Any]:
    """The privacy filter described in the module docstring."""
    out: dict[str, Any] = {}
    for key, value in (data or {}).items():
        key = str(key)
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str):
            if key in SAFE_STR_KEYS:
                out[key] = redact_text(value, values)[:STR_MAX]
            else:
                out[f"{key}_chars"] = len(value)
        elif isinstance(value, (list, tuple, set, dict)):
            out[f"{key}_count"] = len(value)
    return out


class EventLog:
    """Append-only JSONL writer. Thread-safe (subagents log from their own
    thread); a write that fails is dropped, never raised into the run."""

    def __init__(self, path: Path | None = None, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 keep: int = DEFAULT_KEEP, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else log_path()
        self.max_bytes = max(1024, int(max_bytes))
        self.keep = max(1, int(keep))
        self.clock = clock
        self._lock = threading.Lock()
        # Read the secret files once per log, not once per event.
        self._secrets = secret_values()
        self.failed = 0

    def _rotate(self) -> None:
        for index in range(self.keep - 1, 0, -1):
            older = self.path.with_name(f"{self.path.name}.{index}")
            if older.exists():
                os.replace(older, self.path.with_name(f"{self.path.name}.{index + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        last = self.path.with_name(f"{self.path.name}.{self.keep + 1}")
        if last.exists():
            last.unlink()

    def write(self, event: str, *, run_id: str = "", session_id: str = "",
              round_no: int = 0, data: dict[str, Any] | None = None) -> bool:
        if event in SKIPPED_EVENTS:
            return False
        record = {"ts": round(self.clock(), 3), "run_id": run_id,
                  "session_id": session_id, "event": event, "round": int(round_no),
                  "data": scrub(data, self._secrets)}
        line = json.dumps(record, sort_keys=True) + "\n"
        with self._lock:
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                    self._rotate()
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    os.write(fd, line.encode())
                finally:
                    os.close(fd)
                return True
            except OSError:
                self.failed += 1
                return False


def read(path: Path | None = None) -> list[dict[str, Any]]:
    """Every parseable line of the current log file (a torn last line from a
    killed process is skipped, so a reader always sees whole events)."""
    target = Path(path) if path is not None else log_path()
    rows: list[dict[str, Any]] = []
    try:
        text = target.read_text()
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows
