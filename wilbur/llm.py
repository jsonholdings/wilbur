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
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

TOOL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
FENCED_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

# Matches a comma followed by only whitespace/comments before a closing } or ].
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _lenient_json_loads(text: str) -> Any:
    """Parse JSON a local model almost got right.

    Real failures observed from small local models writing tool calls as
    plain text: a trailing comma before the closing brace/bracket
    (`{"a": 1,}`), and single-quoted strings instead of double
    (`{'name': 'read_file'}`). A strict `json.loads` rejects both and the
    whole call is dropped -- this tries strict first (never touches text
    that was already valid) and only rewrites when strict parsing fails.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    repaired = _TRAILING_COMMA_RE.sub(r"\1", text)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # Single-quoted JSON: only attempted if the text has no double quotes at
    # all, so a string that legitimately mixes quote styles (e.g. contains an
    # apostrophe inside a double-quoted value) is never mangled.
    if '"' not in repaired and "'" in repaired:
        swapped = repaired.replace("'", '"')
        try:
            return json.loads(swapped)
        except json.JSONDecodeError:
            pass

    raise json.JSONDecodeError("unparseable after repair attempts", text, 0)


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
            args = _lenient_json_loads(args)
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
                parsed = _lenient_json_loads(match.group(1))
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
                parsed = _lenient_json_loads(content[start:end])
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


class TransientOllamaError(Exception):
    """A retryable failure talking to Ollama: connection reset, timeout, or a
    5xx from the server. 4xx (bad request, model not found, etc.) is never
    wrapped here -- retrying a client error just repeats the same failure."""


class ModelNotResponding(Exception):
    """No bytes arrived from the model server within the stall window.

    Raised instead of retrying: before 2.38.0 a stalled server meant a silent
    wait of up to `timeout` (600 s) per attempt, times 4 attempts, with
    nothing on screen -- which reads as Wilbur having frozen."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__(
            f"model server not responding (no output for {seconds:.0f}s). "
            "Check that Ollama is up, then type /retry to try again.")


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    return (isinstance(exc, urllib.error.URLError)
            and isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)))


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError,
                         ConnectionError, ConnectionResetError)):
        return True
    return False


def tool_call_schema(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """JSON schema for Ollama's `format` field: the reply must be an object
    with a `content` string and a `tool_calls` list whose names are limited to
    the offered tools. Constraining the decode means a weak local model cannot
    emit a malformed call; recover_tool_calls stays as the fallback for models
    that ignore `format` (not yet measured on the abliterated 32b it was
    written for, so it is not removed)."""
    names = sorted({(t.get("function") or t).get("name", "") for t in tools} - {""})
    return {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": names},
                        "arguments": {"type": "object"},
                    },
                    "required": ["name", "arguments"],
                },
            },
        },
        "required": ["content", "tool_calls"],
    }


def parse_constrained_reply(content: str, known: set[str]) -> tuple[list[ToolCall], str] | None:
    """Decode a reply produced under tool_call_schema. None when the content is
    not that shape (the model ignored `format`), so the caller falls back."""
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("tool_calls"), list):
        return None
    calls: list[ToolCall] = []
    for raw in obj["tool_calls"]:
        call = _coerce_call(raw)
        if call is not None and call.name in known:
            calls.append(ToolCall(name=call.name, arguments=call.arguments))
    text = obj.get("content", "")
    return calls, text if isinstance(text, str) else ""


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: int = 600,
        *,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        sleep: Any = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self.constrained_tool_calls = False  # opt-in: Config.constrained_tool_calls
        # Sent on every /api/chat (Config.ollama_keep_alive): the chat request itself keeps the
        # model resident, with the same options it was loaded with, so nothing else has to.
        self.keep_alive: str = "30m"
        # Longest silence (no bytes at all) tolerated from the server before
        # ModelNotResponding: covers prompt reading before the first token,
        # and any gap between streamed chunks. Config.first_token_timeout_s.
        self.stall_timeout: float = 120.0
        # Called as on_progress(phase, info) while a streamed reply arrives:
        # phase is "thinking", "writing" or "done". Set by the Agent.
        self.on_progress: Any = None
        self._clock = time.monotonic

    def _post(self, request: urllib.request.Request) -> dict[str, Any]:
        """Send one request, retrying only transient failures with capped
        exponential backoff (backoff_base * 2**attempt). A 4xx or any other
        non-transient error raises immediately on the first attempt."""
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read())
            except Exception as exc:  # noqa: BLE001 -- classified below
                if not _is_transient(exc) or attempt >= self.max_retries:
                    raise
                self._sleep(self.backoff_base * (2 ** attempt))
                attempt += 1

    def _progress(self, phase: str, **info: Any) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(phase, info)
            except Exception:  # noqa: BLE001 -- display must never break a call
                pass

    def _stream(self, request: urllib.request.Request) -> dict[str, Any]:
        """POST a streaming chat and fold the NDJSON chunks into one
        non-streamed-shaped response.

        The socket timeout is the stall window, so it bounds every silence:
        the wait for the first byte (model load + prompt reading) and each gap
        between chunks. A stall raises ModelNotResponding at once; only a
        failure to connect (or a 5xx) is retried with backoff."""
        attempt = 0
        while True:
            try:
                response = urllib.request.urlopen(request, timeout=self.stall_timeout)
                break
            except Exception as exc:  # noqa: BLE001 -- classified below
                if _is_timeout(exc):
                    raise ModelNotResponding(self.stall_timeout) from exc
                if not _is_transient(exc) or attempt >= self.max_retries:
                    raise
                self._sleep(self.backoff_base * (2 ** attempt))
                attempt += 1

        content: list[str] = []
        thinking: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        final: dict[str, Any] = {}
        phase = ""
        started = self._clock()
        with response:
            while True:
                try:
                    line = response.readline()
                except Exception as exc:  # noqa: BLE001
                    if _is_timeout(exc):
                        raise ModelNotResponding(self.stall_timeout) from exc
                    raise
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError(f"ollama: {chunk['error']}")
                message = chunk.get("message") or {}
                if message.get("thinking"):
                    thinking.append(message["thinking"])
                    if phase != "thinking":
                        phase = "thinking"
                        self._progress("thinking")
                if message.get("content"):
                    content.append(message["content"])
                    if phase != "writing":
                        phase = "writing"
                        self._progress("writing")
                tool_calls.extend(message.get("tool_calls") or [])
                final = chunk  # a server that ignores stream=True sends one object
                if chunk.get("done"):
                    break
                if self.timeout and self._clock() - started > self.timeout:
                    raise ModelNotResponding(self.timeout)
        self._progress("done", prompt_tokens=final.get("prompt_eval_count") or 0)
        return {
            "message": {"content": "".join(content), "thinking": "".join(thinking),
                        "tool_calls": tool_calls},
            "prompt_eval_count": final.get("prompt_eval_count") or 0,
            "eval_count": final.get("eval_count") or 0,
        }

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
            "stream": True,
            "options": {"temperature": temperature, "top_p": top_p, "num_ctx": num_ctx},
        }
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive
        if tools:
            payload["tools"] = tools
        constrained = bool(self.constrained_tool_calls and tools)
        if constrained:
            payload["format"] = tool_call_schema(tools or [])

        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        data = self._stream(request)

        message = data.get("message", {})
        content = message.get("content", "") or ""
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function", raw)
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = _lenient_json_loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args,
                                  call_id=raw.get("id", "")))

        if constrained and not calls:
            names = known_tools or {(t.get("function") or t).get("name", "") for t in tools or []}
            parsed = parse_constrained_reply(content, names)
            if parsed is not None:
                calls, content = parsed
        if not calls and known_tools:
            calls, content = recover_tool_calls(content, known_tools)

        return Reply(
            content=content,
            tool_calls=calls,
            thinking=message.get("thinking", "") or "",
            prompt_tokens=data.get("prompt_eval_count") or 0,
            eval_tokens=data.get("eval_count") or 0,
        )
