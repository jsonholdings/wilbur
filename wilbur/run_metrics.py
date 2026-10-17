"""run.json's `metrics` object: the JSON Holdings shared agent-run record.

Built with `jh_agent_metrics` (the shared agent-metrics asset, pinned in
requirements-gpu-queue.txt's private-build list) so Home Base reads Wilbur and
Claude runs with one schema and one token math. The public build has no JH
package registry, so the import is optional: without it run.json simply has no
`metrics` key and readers fall back to `usage`.

Wilbur talks to ollama, which has no prompt cache, so the cache token fields
stay None (UNKNOWN), never 0. Per-minute rates come from a short ring of
cumulative samples kept per session in this process (the process that owns
the run file) and written alongside the record, so a reader can recompute.
"""
from __future__ import annotations

import threading
import time
from typing import Any

try:
    import jh_agent_metrics as _am
except ImportError:  # public build: no JH registry
    _am = None

# Wilbur run state -> shared schema state.
_STATE_MAP = {
    "working": "running",
    "waiting_approval": "waiting",
    "waiting_input": "waiting",
    "stopped": "stopped",
    "dead": "failed",
}

_samples: dict[str, list] = {}
_lock = threading.Lock()


def available() -> bool:
    return _am is not None


# Subagent registry status (wilbur.tools.agents_registry) -> shared schema state.
_SUB_STATE_MAP = {"running": "running", "done": "done", "error": "failed",
                  "cancelled": "stopped", "timeout": "stopped"}


def _subagent_rows(subagents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Registry rows carry no per-subagent token count, so tokens is None (UNKNOWN)."""
    rows = []
    for sub in subagents or ():
        if not isinstance(sub, dict) or sub.get("id") is None:
            continue
        status = sub.get("state") or sub.get("status")
        rows.append({"id": str(sub["id"]),
                     "state": _SUB_STATE_MAP.get(status, "unknown"),
                     "tokens": sub.get("tokens")})
    return rows


def record(session_id: str, *, state: str, model: str, started_at: float | None,
           usage: dict[str, Any], subagents: list[dict[str, Any]] | None = None,
           now: float | None = None) -> tuple[dict[str, Any], list] | None:
    """(schema-v1 record, sample ring) for this run, or None in the public build."""
    if _am is None:
        return None
    now = time.time() if now is None else now
    tokens = _am.from_ollama_usage(usage.get("prompt_tokens"), usage.get("completion_tokens"))
    with _lock:
        ring = _am.add_sample(_samples.get(session_id, []), now, tokens)
        _samples[session_id] = ring
    per_min, fresh_per_min = _am.per_min_from_samples(ring, now)
    rec = _am.make_record(
        source="wilbur", id=session_id, model=model or None, started_at=started_at,
        elapsed_s=round(now - started_at, 1) if started_at else None,
        tokens=tokens, tokens_per_min=per_min, fresh_per_min=fresh_per_min,
        context=usage.get("context"), calls=usage.get("calls"),
        subagents=_subagent_rows(subagents or []),
        state=_STATE_MAP.get(state, "unknown"),
    )
    return rec, list(ring)


def forget(session_id: str) -> None:
    with _lock:
        _samples.pop(session_id, None)
