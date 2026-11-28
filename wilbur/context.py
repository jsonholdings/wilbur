"""Context accounting and compaction.

A 64K window is the largest that stays fully GPU-resident on a 24GB card, and
a single agent task can exceed it in a dozen tool calls. Compaction is
therefore load-bearing here in a way it is not at 200K: without it the session
either truncates mid-task or spills to CPU and drops ~5x in throughput.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from .state import BRIEFING_MARKER

# Ollama does not expose a tokenizer over HTTP. Qwen-family BPE averages close
# to 3.6 characters per token on source code and command output; 3.5 keeps the
# estimate conservative, which is the direction that matters -- overestimating
# triggers compaction slightly early, underestimating overruns the window.
CHARS_PER_TOKEN = 3.5

# R2 (compact_keep_unacted). Live W3, 2026-11-26: a round of reads was
# compacted to a prose summary before the model had edited those files, so it
# read them all again (7 compactions, 0 edits). Reads the model has not acted on
# are carried through compaction verbatim, up to this share of num_ctx together
# with the recent slice; past that they become a file index so a re-read is
# targeted instead of wholesale. The share stays below compact_at so the carried
# context cannot itself trigger the next compaction.
UNACTED_KEEP_SHARE = 0.50
READ_TOOL = "read_file"
WRITE_TOOLS = frozenset({"edit_file", "write_file", "apply_patch"})


def written_paths(name: str, args: dict[str, Any]) -> list[str]:
    """Every path a write tool call names. write_file/edit_file carry one
    `path`; apply_patch (2.75.0) names several, in its `edits` list or in the
    file headers of its unified `patch`. A diff that does not parse names
    nothing (the tool refuses it too)."""
    if name not in WRITE_TOOLS or not isinstance(args, dict):
        return []
    if name != "apply_patch":
        path = args.get("path")
        return [str(path)] if path else []
    paths: list[str] = []
    edits = args.get("edits")
    if isinstance(edits, list):
        paths = [str(e["path"]) for e in edits if isinstance(e, dict) and e.get("path")]
    patch = args.get("patch")
    if isinstance(patch, str) and patch:
        from .tools.patch import PatchError, parse_unified_diff
        try:
            paths += [fp.path for fp in parse_unified_diff(patch)]
        except PatchError:
            pass
    return list(dict.fromkeys(paths))
_NUMBERED_LINE = re.compile(r"^\s*\d+\t", re.M)

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
    keep_unacted = bool(getattr(config, "compact_keep_unacted", False))
    if keep_unacted:
        keep_recent = _with_newest_round(body, keep_recent)
    # Old harness briefings are never summarised (paraphrase drift); they are
    # dropped, and the agent re-appends the current one on the next call.
    to_summarise = [m for m in body[: len(body) - len(keep_recent)]
                    if not (m.get("role") == "user"
                            and BRIEFING_MARKER in (m.get("content") or ""))]
    if not to_summarise:
        return messages
    carried: list[dict[str, Any]] = []
    if keep_unacted:
        to_summarise, carried = _carry_unacted_reads(body, to_summarise, keep_recent, config)

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
        # With compact_keep_unacted the tail also reaches back to the newest
        # round, so a large round is never cut by the 12-message fallback.
        start = max(0, len(body) - 12)
        if keep_unacted:
            start = min(start, len(body) - len(keep_recent))
        return system + _drop_orphan_tools(body[start:])

    marker = {"role": "user",
              "content": f"[Earlier conversation, compacted]\n\n{summary}"}
    ack = {"role": "assistant",
           "content": "Understood. Continuing from those notes."}
    return system + [marker, ack] + carried + keep_recent


def _with_newest_round(body: list[dict[str, Any]], keep: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extend the kept tail back to the newest assistant tool-call message, so
    the newest round's results are never summarised before the model sees them."""
    start = len(body) - len(keep)
    for index in range(len(body) - 1, -1, -1):
        msg = body[index]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            return body[index:] if index < start else keep
    return keep


def _norm_path(path: Any) -> str:
    return os.path.normpath(str(path or ""))


def _call_args(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    fn = call.get("function") or {}
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return fn.get("name", ""), args if isinstance(args, dict) else {}


def _paired_calls(body: list[dict[str, Any]]) -> dict[int, tuple[str, dict[str, Any]]]:
    """Map id(tool message) -> (tool name, arguments) of the call that made it.
    Tool messages follow their assistant message in call order."""
    pairs: dict[int, tuple[str, dict[str, Any]]] = {}
    pending: list[tuple[str, dict[str, Any]]] = []
    for msg in body:
        role = msg.get("role")
        if role == "assistant":
            pending = [_call_args(c) for c in (msg.get("tool_calls") or [])]
        elif role == "tool" and pending:
            pairs[id(msg)] = pending.pop(0)
    return pairs


def _carry_unacted_reads(body, to_summarise, keep_recent, config):
    """Split `to_summarise` into (still to summarise, carried messages).

    A read_file result is unacted when no later edit/write call names the same
    path and the path is not read again later. Unacted reads are carried
    verbatim, newest first, while they fit; the rest become a file index."""
    pairs = _paired_calls(body)
    written_after: set[str] = set()
    read_after: set[str] = set()
    unacted: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    in_summary = {id(m) for m in to_summarise}
    for msg in reversed(body):
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                name, args = _call_args(call)
                for written in written_paths(name, args):
                    written_after.add(_norm_path(written))
            continue
        pair = pairs.get(id(msg))
        if msg.get("role") != "tool" or not pair or pair[0] != READ_TOOL:
            continue
        path = _norm_path(pair[1].get("path"))
        content = msg.get("content") or ""
        if (id(msg) in in_summary and path not in written_after and path not in read_after
                and _NUMBERED_LINE.search(content)):
            unacted.append((msg, path, pair[1]))
        read_after.add(path)
    if not unacted:
        return to_summarise, []

    room = int(config.num_ctx * UNACTED_KEEP_SHARE) - estimate_tokens(keep_recent)
    verbatim: list[str] = []
    index: list[str] = []
    for msg, path, args in unacted:  # newest first
        content = msg.get("content") or ""
        cost = estimate_tokens([msg]) + 16
        lines = len(_NUMBERED_LINE.findall(content))
        first = int(args.get("offset", 1) or 1)
        span = f"lines {first}-{first + lines - 1}"
        if cost <= room:
            room -= cost
            verbatim.append(f"--- {path} ({span}) ---\n{content}")
        else:
            index.append(f"- {path}: {span} read; re-read with offset/limit when you need it")
    carried_ids = {id(m) for m, _, _ in unacted}
    remaining = [m for m in to_summarise if id(m) not in carried_ids]
    parts: list[str] = []
    if verbatim:
        parts.append("[Kept through compaction: files you read and have not edited yet. "
                     "Their contents are below; do not read them again.]\n\n"
                     + "\n\n".join(reversed(verbatim)))
    if index:
        parts.append("[File index: read before compaction and not kept, to save context. "
                     "Re-read only the one you work on next.]\n" + "\n".join(reversed(index)))
    return remaining, [{"role": "user", "content": "\n\n".join(parts)}]


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
