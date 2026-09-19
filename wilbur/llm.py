"""Ollama chat client with tool-call recovery.

Local models frequently emit a structurally correct tool call as plain text
instead of through the structured channel -- either as bare JSON or wrapped in
<tool_call> tags the server failed to parse. Measured on
huihui_ai/qwen2.5-coder-abliterate:32b, which returns

    {"name": "run_bash", "arguments": {"command": "wc -l /etc/hostname"}}

as message content with tool_calls empty. Dropping those turns a working model
into a broken one, so the client recovers them rather than discarding them.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

TOOL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
FENCED_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str = ""
    recovered: bool = False   # True when parsed out of content, not the API field


@dataclass
class Reply:
    content: str
    tool_calls: list[ToolCall]
    thinking: str = ""
    prompt_tokens: int = 0
    eval_tokens: int = 0


def _balanced_json_objects(text: str) -> Iterator[tuple[int, int]]:
    """Yield (start, end) of each top-level {...} span, respecting strings.

    Positions, not substrings: the caller needs exact offsets to delete a
    recovered call from the message body. Re-finding a substring collapses
    duplicate identical calls onto the first occurrence and corrupts the
    cleaned text.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield start, i + 1
                    start = -1


def _coerce_call(obj: Any) -> ToolCall | None:
    if not isinstance(obj, dict):
        return None
    # Accept both {"name":..,"arguments":..} and OpenAI-style nesting.
    if "function" in obj and isinstance(obj["function"], dict):
        obj = obj["function"]
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    if not name or not isinstance(name, str):
        return None
    args = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    if not isinstance(args, dict):
        return None
    return ToolCall(name=name, arguments=args, recovered=True)


def recover_tool_calls(content: str, known: set[str]) -> tuple[list[ToolCall], str]:
    """Extract tool calls a model wrote into its message body.

    Returns (calls, cleaned_content). Only calls naming a tool that actually
    exists are recovered -- otherwise ordinary prose containing JSON, or a code
    block the user asked for, would be misread as a call to run.
    """
    if not content or "{" not in content:
        return [], content
    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []

    for pattern in (TOOL_TAG_RE, FENCED_RE):
        for match in pattern.finditer(content):
            try:
                parsed = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            call = _coerce_call(parsed)
            if call and call.name in known:
                calls.append(call)
                spans.append(match.span())

    if not calls:
        # Bare JSON with no wrapper -- the abliterated-32b failure mode.
        for start, end in _balanced_json_objects(content):
            try:
                parsed = json.loads(content[start:end])
            except json.JSONDecodeError:
                continue
            call = _coerce_call(parsed)
            if call and call.name in known:
                calls.append(call)
                spans.append((start, end))

    cleaned = content
    for start, end in sorted(spans, reverse=True):
        cleaned = cleaned[:start] + cleaned[end:]
    return calls, cleaned.strip()


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: int = 600) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        known_tools: set[str] | None = None,
        num_ctx: int = 65536,
        temperature: float = 0.15,
        top_p: float = 0.9,
    ) -> Reply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "top_p": top_p, "num_ctx": num_ctx},
        }
        if tools:
            payload["tools"] = tools

        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read())

        message = data.get("message", {})
        content = message.get("content", "") or ""
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function", raw)
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args,
                                  call_id=raw.get("id", "")))

        if not calls and known_tools:
            calls, content = recover_tool_calls(content, known_tools)

        return Reply(
            content=content,
            tool_calls=calls,
            thinking=message.get("thinking", "") or "",
            prompt_tokens=data.get("prompt_eval_count", 0),
            eval_tokens=data.get("eval_count", 0),
        )
