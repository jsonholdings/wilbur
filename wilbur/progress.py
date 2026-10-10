"""The progress log a long run leaves for the user to read in the morning.

A plain, append-only Markdown file next to the session's JSON, one entry per
event (started, checkpoint at a round cap, auto-continue, committed checkpoint,
stopped) with a timestamp. It is for the human, not the model: it never goes
back into the transcript.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import config as _config


def log_path(session_id: str) -> Path:
    # Read SESSION_DIR at call time so tests that redirect it (and a user who
    # sets WILBUR_SESSIONS) are honoured.
    return _config.SESSION_DIR / f"{session_id}.progress.md"


def append(session_id: str, event: str, detail: str = "") -> Path:
    path = log_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"- **{stamp}** — {event}"
    if detail:
        body = detail.strip().replace("\n", "\n  ")
        line += f"\n  {body}"
    with path.open("a") as fh:
        fh.write(line + "\n")
    return path
