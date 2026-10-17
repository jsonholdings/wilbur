"""Authoritative run state: `<sessions>/<id>.run.json`.

Why this exists (owner, 2026-10-16): a run that stopped at its prompt looked
alive for 3h20m because nothing outside the terminal said otherwise. The
watchdog inferred IDLE from session-file mtime and wrote to a log nobody
opened. This file is the truth: the Repl writes it atomically on every state
transition, and `wilbur status` reads it instead of guessing.

States: `working`, `waiting_approval`, `waiting_input`, `stopped`.
Stop reasons (shared with progress.md): `done`, `horizon`, `stuck`,
`empty_reply`, `round_cap`, `time_cap`, `count_cap`, `refused`, `question`,
`user_interrupt`, `error`.

A file whose pid is dead reads as `dead` -- the run is gone, but the file
tells you how it ended. Removed on clean exit.

Outside readers (the Home Base Agents board, 2026-10-16) also need elapsed
time, tokens and subagents, so run.json carries `started_at` (the run's first
state write) and `usage` (cumulative prompt/completion/total tokens, model
calls and model seconds), and `<id>.subagents.json` mirrors the in-process
subagent registry (`wilbur.tools.agents_registry`), written atomically on
every subagent transition. Both files are removed on clean exit.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import config as _config
from . import run_metrics

# --- states ---
WORKING = "working"
WAITING_APPROVAL = "waiting_approval"
WAITING_INPUT = "waiting_input"
STOPPED = "stopped"
DEAD = "dead"

# --- stop reasons (enum shared with progress.md) ---
DONE = "done"
HORIZON = "horizon"
STUCK = "stuck"
EMPTY_REPLY = "empty_reply"
ROUND_CAP = "round_cap"
TIME_CAP = "time_cap"
COUNT_CAP = "count_cap"
REFUSED = "refused"
QUESTION = "question"
USER_INTERRUPT = "user_interrupt"
ERROR = "error"

STOP_REASONS = (
    DONE, HORIZON, STUCK, EMPTY_REPLY, ROUND_CAP, TIME_CAP, COUNT_CAP,
    REFUSED, QUESTION, USER_INTERRUPT, ERROR,
)


def _sessions_dir() -> Path:
    # Read at call time so tests that redirect SESSION_DIR are honoured.
    return _config.SESSION_DIR


def surface() -> str:
    """Which front end launched this run: "vscode" when the VS Code extension
    set WILBUR_SURFACE=vscode on its terminal, else "cli"."""
    return "vscode" if os.environ.get("WILBUR_SURFACE", "").strip().lower() == "vscode" else "cli"


def path(session_id: str) -> Path:
    return _sessions_dir() / f"{session_id}.run.json"


def write(session_id: str, *, state: str, reason: str | None = None,
          pid: int | None = None, cwd: str = "", model: str = "",
          version: str = "", rounds: int = 0,
          last_progress_at: float | None = None,
          started_at: float | None = None, since: float | None = None,
          usage: dict[str, Any] | None = None,
          unread_message_since: float | None = None) -> Path | None:
    """Atomically write the run state file. Returns the path, or None on
    failure (a run-state write that fails must never take down the run)."""
    try:
        from . import __version__
        ver = version or __version__
    except Exception:
        ver = version
    try:
        d = _sessions_dir()
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": pid if pid is not None else os.getpid(),
            "session_id": session_id,
            "cwd": cwd,
            "state": state,
            "reason": reason,
            "since": since if since is not None else time.time(),
            "last_progress_at": last_progress_at if last_progress_at is not None else time.time(),
            "rounds": rounds,
            "model": model,
            "version": ver,
            "started_at": started_at,
            "surface": surface(),
            "usage": usage if usage is not None else dict(EMPTY_USAGE),
            # Oldest user message typed mid-turn and not yet read (wilbur.inbox);
            # `wilbur status --check` fails when it is older than 2x the bound.
            "unread_message_since": unread_message_since,
        }
        # The shared agent-metrics record (private build; see wilbur/run_metrics.py).
        # A metrics fault (bad usage/subagent row, module error) must never cost the run.json write.
        try:
            built = run_metrics.record(session_id, state=state, model=model, started_at=started_at,
                                       usage=payload["usage"], subagents=read_subagents(session_id))
        except Exception:  # noqa: BLE001 -- metrics are optional; the state write is not
            built = None
        if built is not None:
            payload["metrics"], payload["metrics_samples"] = built
        p = path(session_id)
        _atomic_write(p, payload)
        return p
    except OSError:
        return None


EMPTY_USAGE: dict[str, Any] = {
    "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    "context": None, "calls": 0, "seconds": 0.0,
}


def usage_from_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Map the Repl's `/cost` counters onto run.json's `usage` object."""
    prompt = int(stats.get("in_tokens", 0) or 0)
    completion = int(stats.get("out_tokens", 0) or 0)
    context = stats.get("last_prompt_tokens")
    return {
        "prompt_tokens": prompt, "completion_tokens": completion,
        "total_tokens": prompt + completion,
        # The last model call's prompt size; None (UNKNOWN) before the first call.
        "context": int(context) if isinstance(context, (int, float)) and not isinstance(context, bool) else None,
        "calls": int(stats.get("calls", 0) or 0),
        "seconds": round(float(stats.get("seconds", 0.0) or 0.0), 3),
    }


def _atomic_write(p: Path, payload: Any) -> None:
    # A per-process tmp name: subagent threads and the Repl may write the
    # same file at once, and a shared tmp would interleave their bytes.
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(p)  # atomic: a half-written state file is worse than none


def subagents_path(session_id: str) -> Path:
    return _sessions_dir() / f"{session_id}.subagents.json"


def write_subagents(session_id: str, records: list[dict[str, Any]]) -> Path | None:
    """Atomically write the subagent list. None on failure (never fatal)."""
    try:
        d = _sessions_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = subagents_path(session_id)
        _atomic_write(p, {"session_id": session_id, "updated_at": time.time(),
                          "subagents": records})
        return p
    except OSError:
        return None


def read_subagents(session_id: str) -> list[dict[str, Any]]:
    """The persisted subagent list, or [] when absent or unreadable."""
    try:
        data = json.loads(subagents_path(session_id).read_text())
    except (OSError, ValueError):
        return []
    subs = data.get("subagents") if isinstance(data, dict) else None
    return subs if isinstance(subs, list) else []


def read(session_id: str) -> dict[str, Any] | None:
    """Read the run state file. A file whose pid is dead reads as `dead`
    (the original state is preserved under `original_state`). Returns None
    if the file is absent or unreadable."""
    p = path(session_id)
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    _mark_dead(data)
    return data


def _mark_dead(data: dict[str, Any]) -> None:
    pid = data.get("pid")
    if pid is not None and not _is_wilbur(pid):
        data["original_state"] = data.get("state")
        data["state"] = DEAD


def _is_wilbur(pid: int) -> bool:
    """Alive AND still a wilbur process. A recycled pid that now belongs to
    something else must read as dead, not as a live run. Where /proc is not
    available the cmdline check is skipped (liveness only)."""
    if not _pid_alive(pid):
        return False
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except (OSError, ValueError):
        return True
    return b"wilbur" in raw or int(pid) == os.getpid()


def remove(session_id: str) -> bool:
    """Remove the run state file (clean exit). Returns True if a file was
    actually removed, False if there was nothing to remove."""
    try:
        subagents_path(session_id).unlink()
    except OSError:
        pass
    run_metrics.forget(session_id)
    p = path(session_id)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists. Uses os.kill(pid, 0) which
    raises ProcessLookupError for a dead pid and PermissionError for a live
    pid owned by another user (still alive)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def all_run_files() -> list[Path]:
    """Every *.run.json in the sessions dir, sorted by mtime (newest first).

    A missing dir means no runs ([]). An unreadable dir RAISES OSError:
    `wilbur status --check` must report UNKNOWN there, never "nothing
    running" -- a check that could not look is not a check that found
    nothing. (Path.glob swallows PermissionError, so list with os.scandir.)"""
    d = _sessions_dir()
    if not d.exists():
        return []
    with os.scandir(d) as it:
        files = [Path(e.path) for e in it if e.name.endswith(".run.json")]

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    files.sort(key=_mtime, reverse=True)
    return files


def read_all() -> list[dict[str, Any]]:
    """Read every run.json file. Returns a list of dicts (one per file),
    each with the file's contents plus `path` (str). Files whose pid is
    dead are marked `state: "dead"` with `original_state` preserved."""
    out: list[dict[str, Any]] = []
    for p in all_run_files():  # OSError propagates: unreadable dir = UNKNOWN
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        _mark_dead(data)
        data["path"] = str(p)
        sid = data.get("session_id")
        data["subagents"] = read_subagents(sid) if sid else []
        out.append(data)
    return out


# --- reasons from keep-going decisions -------------------------------------

_DECISION_REASONS = {
    "cancelled": USER_INTERRUPT,
    "empty reply": EMPTY_REPLY,
    "asked you a question": QUESTION,
    "holding": QUESTION,
    "all items done": DONE,
    "finished": DONE,
}


def reason_from_decision(text: str) -> str:
    """Map a keepgoing.Decision reason to the shared stop-reason enum."""
    return _DECISION_REASONS.get(text, DONE)


# --- loud stop in the terminal ----------------------------------------------

def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def stop_banner(reason: str, since: float) -> str:
    return (f"STOPPED \u2014 {reason} \u2014 idle since {_hhmm(since)} "
            f"\u2014 type continue to resume")


TITLE_GLYPH = "\U0001F416"   # pig: the portable icon where OSC 1 is ignored
TITLE_PUSH = "\x1b[22;0t"    # xterm title stack: save icon + window title
TITLE_POP = "\x1b[23;0t"     # restore them (also covers crash paths via finally)


def title_seq(text: str) -> str:
    """OSC 2 (window/tab title) plus OSC 1 (icon name)."""
    text = text.replace("\x1b", "").replace("\x07", "")
    return f"\x1b]2;{text}\x07\x1b]1;{text}\x07"


def identity(cwd: str = "", model: str = "") -> str:
    parts = [f"{TITLE_GLYPH} Wilbur"]
    base = os.path.basename(str(cwd).rstrip("/")) if cwd else ""
    if base:
        parts.append(base)
    if model:
        parts.append(model)
    return " \u00b7 ".join(parts)


def titles_enabled(stream=None) -> bool:
    stream = stream if stream is not None else sys.stdout
    if os.environ.get("TERM", "") in ("", "dumb"):
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def start_title(cwd: str = "", model: str = "") -> str:
    return TITLE_PUSH + title_seq(identity(cwd, model))


def restore_title() -> str:
    return TITLE_POP


def working_title(rounds: int) -> str:
    return title_seq(f"{TITLE_GLYPH} Wilbur: working ({rounds})")


def stopped_title(reason: str, since: float) -> str:
    return title_seq(f"{TITLE_GLYPH} Wilbur: stopped ({reason}) {_hhmm(since)}")


# Idle-exit process exit codes, one per stop reason, so a supervisor can tell
# how the run ended without reading the log. 3 is the fallback.
IDLE_EXIT_CODES = {
    DONE: 0,
    HORIZON: 3,
    STUCK: 4,
    EMPTY_REPLY: 5,
    ROUND_CAP: 6,
    TIME_CAP: 7,
    COUNT_CAP: 8,
    REFUSED: 9,
    QUESTION: 10,
    USER_INTERRUPT: 11,
    ERROR: 12,
}
