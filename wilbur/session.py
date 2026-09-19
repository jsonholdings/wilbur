"""Save and reload a session, so a long job survives closing the terminal.

`config.SESSION_DIR` has existed since v2 was written and nothing ever used it:
a constant that promises persistence the code does not implement. This is that
promise kept.

What is saved is the transcript plus the harness's run state -- objective, plan
and failure ledger. Reloading only the transcript would restore the words and
lose the thread, which on a local model is most of what matters.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import SESSION_DIR
from .state import RunState

# A transcript is the whole point, but an unbounded one turns resume into a
# context overflow on the first turn. The tail is what a resumed session needs.
MAX_SAVED_MESSAGES = 400


def _slug(cwd: str) -> str:
    return Path(cwd).name.replace(" ", "-") or "session"


def save(session_id: str, cwd: str, messages: list[dict[str, Any]],
         state: RunState, model: str) -> Path | None:
    """Write the session. Returns the path, or None if it could not be written.

    Never raises: losing a session file is an annoyance, but taking down a
    working agent because a disk is full is not an acceptable trade.
    """
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        path = SESSION_DIR / f"{session_id}.json"
        payload = {
            "id": session_id,
            "cwd": cwd,
            "model": model,
            "updated": time.time(),
            "objective": state.objective,      # duplicated for cheap listing
            "state": state.to_dict(),
            "messages": messages[-MAX_SAVED_MESSAGES:],
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)                       # atomic; a half-written session
        return path                             # is worse than none at all
    except OSError:
        return None


def load(session_id: str) -> dict[str, Any] | None:
    path = SESSION_DIR / f"{session_id}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    data["state"] = RunState.from_dict(data.get("state") or {})
    return data


def latest(cwd: str | None = None) -> dict[str, Any] | None:
    """Most recently updated session, optionally restricted to one directory."""
    best = None
    for path in _files():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if cwd and data.get("cwd") != cwd:
            continue
        if best is None or data.get("updated", 0) > best.get("updated", 0):
            best = data
    if best is not None:
        best["state"] = RunState.from_dict(best.get("state") or {})
    return best


def listing(limit: int = 20) -> list[dict[str, Any]]:
    rows = []
    for path in _files():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        rows.append({
            "id": data.get("id", path.stem),
            "cwd": data.get("cwd", ""),
            "updated": data.get("updated", 0),
            "objective": (data.get("objective") or "").strip().splitlines()[:1],
            "turns": len(data.get("messages") or []),
        })
    rows.sort(key=lambda r: r["updated"], reverse=True)
    return rows[:limit]


def forget(session_id: str) -> bool:
    """Delete one saved session so it can never be auto-picked by `--continue`
    or shown again by `--sessions`. Returns True if a file was actually
    removed, False if there was nothing to remove (never raises for a missing
    or already-gone file -- the caller only cares whether it is now gone).
    """
    path = SESSION_DIR / f"{session_id}.json"
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def new_id(cwd: str) -> str:
    return f"{_slug(cwd)}-{int(time.time())}"


def _files() -> list[Path]:
    try:
        return sorted(SESSION_DIR.glob("*.json"))
    except OSError:
        return []
