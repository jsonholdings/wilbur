"""Context accounting and compaction.

A 64K window is the largest that stays fully GPU-resident on a 24GB card, and
a single agent task can exceed it in a dozen tool calls. Compaction is
therefore load-bearing here in a way it is not at 200K: without it the session
either truncates mid-task or spills to CPU and drops ~5x in throughput.
"""
from __future__ import annotations

import json
from typing import Any

from .state import BRIEFING_MARKER

# Ollama does not expose a tokenizer over HTTP. Qwen-family BPE averages close
# to 3.6 characters per token on source code and command output; 3.5 keeps the
# estimate conservative, which is the direction that matters -- overestimating
# triggers compaction slightly early, underestimating overruns the window.
CHARS_PER_TOKEN = 3.5

COMPACT_INSTRUCTION = """Summarise the conversation so far for your own use as \
the same agent continuing this task. Write it as notes to yourself, not as a \
report to the user.

Cover, in this order:
1. What the user asked for, in their own terms.
2. What has been done already -- files created or changed, commands run and
   their outcomes. Name exact paths.
3. What was learned about the codebase that would be expensive to rediscover.
4. What remains to be done, and the immediate next step.

Be specific and keep every fact that is still needed. Omit superseded attempts \
and tool output that no longer matters."""


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        total += len(msg.get("content") or "") / CHARS_PER_TOKEN
        total += len(msg.get("tool_name") or "") / CHARS_PER_TOKEN
        for call in msg.get("tool_calls") or []:
            total += len(json.dumps(call)) / CHARS_PER_TOKEN
        total += 4  # per-message role and delimiter overhead
    return int(total)


def compact(messages: list[dict[str, Any]], client, config) -> list[dict[str, Any]]:
    """Replace the middle of the history with a summary.

    The system prompt and the most recent exchanges are preserved verbatim --
    recent tool results are what the model is actively reasoning over, and
    summarising them is what makes a compacted agent start repeating work.
    """
    # The pinned head is the system prompt plus any harness briefing that
    # follows it. Both are regenerated every turn and must never be summarised:
    # paraphrasing the objective is the exact drift this guards against.
    head = 0
    if messages and messages[0]["role"] == "system":
        head = 1
        while (head < len(messages) and messages[head]["role"] == "system"
               and BRIEFING_MARKER in (messages[head].get("content") or "")):
            head += 1
    system = messages[:head]
    body = messages[head:]
    if len(body) <= 6:
        return messages

    keep_recent = _recent_slice(body, config)
    to_summarise = body[: len(body) - len(keep_recent)]
    if not to_summarise:
        return messages

    transcript = _render(to_summarise)
    try:
        reply = client.chat(
            [{"role": "system", "content": "You compact agent transcripts."},
             {"role": "user", "content": f"{COMPACT_INSTRUCTION}\n\n---\n{transcript}"}],
            None,
            num_ctx=config.num_ctx,
            temperature=0.0,
        )
        summary = reply.content.strip()
    except Exception:
        summary = ""

    if not summary:
        # Never silently drop history. If the summariser failed, keep more of
        # the tail rather than returning a session that has forgotten its task.
        return system + _drop_orphan_tools(body[-12:])

    marker = {"role": "user",
              "content": f"[Earlier conversation, compacted]\n\n{summary}"}
    ack = {"role": "assistant",
           "content": "Understood. Continuing from those notes."}
    return system + [marker, ack] + keep_recent


def _recent_slice(body: list[dict[str, Any]], config) -> list[dict[str, Any]]:
    """Take recent messages up to a third of the budget, without splitting a
    tool call away from the assistant message that made it."""
    limit = int(config.num_ctx * 0.30)
    kept: list[dict[str, Any]] = []
    running = 0
    for msg in reversed(body):
        cost = estimate_tokens([msg])
        if running + cost > limit and len(kept) >= 4:
            break
        kept.insert(0, msg)
        running += cost
    return _drop_orphan_tools(kept)


def _drop_orphan_tools(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip leading tool results whose assistant call is no longer present.

    A history that opens on a tool message asks the model to interpret output
    from a call it cannot see.
    """
    out = list(msgs)
    while out and out[0].get("role") == "tool":
        out.pop(0)
    return out


def _render(messages: list[dict[str, Any]]) -> str:
    lines = []
    for msg in messages:
        role = msg["role"]
        if role == "tool" and msg.get("tool_name"):
            role = f"tool:{msg['tool_name']}"
        content = (msg.get("content") or "").strip()
        calls = msg.get("tool_calls") or []
        if calls:
            names = ", ".join(c.get("function", {}).get("name", "?") for c in calls)
            content = (content + f"\n[called: {names}]").strip()
        if content:
            lines.append(f"{role.upper()}: {content[:3000]}")
    return "\n\n".join(lines)
