"""Opt-in critic pass: one extra model call reviews a write before it is accepted.

Forced verification catches what the test suite catches; the critic catches the
edit that satisfies the letter of the instruction while breaking something
adjacent. Off by default (`Config.critic_pass`), since it costs roughly one
extra model call per write.
"""
from __future__ import annotations

import json
from typing import Any

CRITIC_PROMPT = (
    "You are reviewing a single file change made by a coding agent, before it is "
    "accepted. Look for a change that does what was asked but breaks something "
    "adjacent: a removed line still needed, a renamed name left referenced, wrong "
    "indentation, a syntax error, an off-by-one. Reply with exactly `OK` if you "
    "see no concrete problem, otherwise `PROBLEM: <one or two sentences>`. Do not "
    "suggest style changes."
)
MAX_CHANGE_CHARS = 6000


def review(client: Any, config: Any, tool_name: str, arguments: dict[str, Any],
           result_text: str) -> str | None:
    """Return a harness note when the critic finds a problem, else None.

    Never raises: a failed critic call must not fail a write that succeeded.
    """
    change = json.dumps({"tool": tool_name, "arguments": arguments},
                        default=str)[:MAX_CHANGE_CHARS]
    messages = [
        {"role": "system", "content": CRITIC_PROMPT},
        {"role": "user", "content": f"Change:\n{change}\n\nTool result:\n"
                                    f"{result_text[:MAX_CHANGE_CHARS]}"},
    ]
    try:
        reply = client.chat(
            messages, None, known_tools=set(),
            num_ctx=config.num_ctx,
            temperature=0.0,
            top_p=config.top_p,
        )
    except Exception:  # noqa: BLE001 - the critic is advisory only
        return None
    verdict = (getattr(reply, "content", "") or "").strip()
    if not verdict.upper().startswith("PROBLEM"):
        return None
    detail = verdict.split(":", 1)[1].strip() if ":" in verdict else verdict
    return f"[harness critic flagged this change] {detail} Re-read the file and fix it if the critic is right."
