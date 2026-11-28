"""OpenAI-compatible chat backend (`backend: "openai"`, ADR-0004 decision 3).

Lets llama.cpp `llama-server`, vLLM or SGLang serve the model through
POST {base_url}/v1/chat/completions, with Ollama staying the default. It
subclasses OllamaClient so the streaming machinery is shared, not copied: the
reader thread, the stall window, the busy probe hook, connect retries with
backoff, abort-on-cancel and the think budget all behave the same. Only the
wire format differs:

- request: OpenAI messages (tool-call arguments as JSON strings, ids paired
  to tool results), `max_tokens` for num_predict, `response_format` json_schema
  for the R1 constrained retry, `chat_template_kwargs.enable_thinking=false`
  for think=False (llama.cpp, vLLM and SGLang all read it).
- response: SSE `data:` lines; tool calls arrive as indexed fragments whose
  `arguments` strings are concatenated; reasoning arrives as
  `reasoning_content` (llama.cpp, vLLM) or `reasoning` (SGLang, newer vLLM).
- metrics: `usage` (requested with stream_options.include_usage) maps onto the
  same Reply.prompt_tokens / eval_tokens Ollama fills, and llama.cpp's
  `timings` onto Ollama's *_duration nanosecond fields in `last_metrics`, so
  bench rows stay comparable. A server that sends no usage reports 0, the same
  as Ollama's null prompt_eval_count.

num_ctx is a server launch flag on these engines (llama-server -c,
vLLM --max-model-len), not a per-request option, so it is not sent.
"""
from __future__ import annotations

import json
import queue
import stat
import threading
import urllib.request
from pathlib import Path
from typing import Any

from .llm import (
    ModelNotResponding,
    OllamaClient,
    Reply,
    ToolCall,
    TransientOllamaError,
    _is_timeout,
    _lenient_json_loads,
    parse_constrained_reply,
    recover_tool_calls,
    tool_call_schema,
)

# OpenAI finish_reason -> the Ollama done_reason the Reply flags read.
_FINISH_TO_DONE = {"length": "length", "stop": "stop", "tool_calls": "stop",
                   "function_call": "stop", "content_filter": "stop"}


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ollama-shaped history (what the Agent keeps) -> OpenAI chat messages.

    Assistant tool calls get `type`, a string `arguments` and an id; a tool
    result without `tool_call_id` is paired, in order, with the ids of the
    assistant turn before it (strict servers such as vLLM reject an unpaired
    tool message). Ollama's `images` list becomes image_url content parts."""
    out: list[dict[str, Any]] = []
    pending_ids: list[str] = []
    counter = 0
    for msg in messages:
        role = msg.get("role", "user")
        new: dict[str, Any] = {"role": role, "content": msg.get("content", "") or ""}
        images = msg.get("images") or []
        if images and role == "user":
            parts: list[dict[str, Any]] = [{"type": "text", "text": new["content"]}]
            for img in images:
                url = img if str(img).startswith("data:") else f"data:image/png;base64,{img}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
            new["content"] = parts
        if role == "assistant" and msg.get("tool_calls"):
            calls = []
            pending_ids = []
            for raw in msg["tool_calls"]:
                fn = raw.get("function", raw)
                args = fn.get("arguments", {})
                call_id = raw.get("id") or f"call_{counter}"
                counter += 1
                pending_ids.append(call_id)
                calls.append({"id": call_id, "type": "function", "function": {
                    "name": fn.get("name", ""),
                    "arguments": args if isinstance(args, str) else json.dumps(args)}})
            new["tool_calls"] = calls
        if role == "tool":
            call_id = msg.get("tool_call_id") or (pending_ids.pop(0) if pending_ids else "")
            if call_id in pending_ids:
                pending_ids.remove(call_id)
            if call_id:
                new["tool_call_id"] = call_id
            if msg.get("tool_name"):
                new["name"] = msg["tool_name"]
        out.append(new)
    return out


class OpenAIClient(OllamaClient):
    """Same interface as OllamaClient (chat -> Reply); see the module doc."""

    def __init__(self, *args: Any, api_key: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.api_key = api_key
        # Ollama-named metrics of the last reply (prompt_eval_count, eval_count,
        # prompt_eval_duration, eval_duration in ns when the server sent timings).
        self.last_metrics: dict[str, Any] = {}

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if bool(self.api_key):
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _stream(self, request: urllib.request.Request, think_budget: int = 0) -> dict[str, Any]:
        """Fold an SSE chat-completions stream into the Ollama-shaped dict
        OllamaClient._stream returns; same stall, busy and abort handling."""
        out: queue.Queue[tuple[str, Any]] = queue.Queue()
        stop = threading.Event()
        holder: dict[str, Any] = {"lock": threading.Lock(), "response": None}
        read_timeout = self.stall_timeout + max(0.0, self.stall_busy_max_s)
        reader = threading.Thread(
            target=self._read_lines, args=(request, read_timeout, out, stop, holder),
            name="wilbur-stream", daemon=True)
        reader.start()

        content: list[str] = []
        thinking: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        timings: dict[str, Any] = {}
        finish = ""
        thinking_cut = False
        phase = ""
        last_byte = self._clock()
        try:
            while True:
                try:
                    kind, item = out.get(timeout=self.stall_timeout)
                except queue.Empty:
                    silent = self._clock() - last_byte
                    over_idle = bool(self.timeout) and silent > self.timeout
                    if over_idle or not self._still_working(silent):
                        raise ModelNotResponding(silent) from None
                    phase = "busy"
                    self._progress("busy", silent_s=int(silent))
                    continue
                if kind == "error":
                    if _is_timeout(item):
                        raise ModelNotResponding(self._clock() - last_byte) from item
                    raise item
                last_byte = self._clock()
                line = item
                if not line:
                    break
                line = line.decode("utf-8", "replace").strip() if isinstance(line, bytes) else line.strip()
                if not line or line.startswith(":"):
                    continue  # blank separator or SSE comment / keep-alive
                if line.startswith("data:"):
                    line = line[5:].strip()
                elif line.startswith(("event:", "id:", "retry:")):
                    continue
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except ValueError as exc:
                    raise TransientOllamaError(
                        f"model server sent a malformed stream line ({exc.msg} at char "
                        f"{exc.pos}): {line[:80]!r}. Type /retry to try again.") from None
                if not isinstance(chunk, dict):
                    raise TransientOllamaError(
                        f"model server sent a non-object stream line: {line[:80]!r}. "
                        "Type /retry to try again.")
                if chunk.get("error"):
                    err = chunk["error"]
                    raise RuntimeError(f"openai backend: {err.get('message', err) if isinstance(err, dict) else err}")
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                if isinstance(chunk.get("timings"), dict):
                    timings = chunk["timings"]
                for choice in chunk.get("choices") or []:
                    # A server that ignores stream=True sends one `message`.
                    delta = choice.get("delta") or choice.get("message") or {}
                    reason = delta.get("reasoning_content") or delta.get("reasoning")
                    if reason:
                        thinking.append(reason)
                        if phase != "thinking":
                            phase = "thinking"
                            self._progress("thinking")
                    if delta.get("content"):
                        content.append(delta["content"])
                        if phase != "writing":
                            phase = "writing"
                            self._progress("writing")
                        if self.on_content is not None:
                            self.on_content(delta["content"])
                    for pos, frag in enumerate(delta.get("tool_calls") or []):
                        index = frag.get("index", pos)
                        slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if frag.get("id"):
                            slot["id"] = frag["id"]
                        fn = frag.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        args = fn.get("arguments")
                        if isinstance(args, str):
                            slot["arguments"] += args
                        elif isinstance(args, dict):
                            slot["arguments"] = json.dumps(args)
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                if (think_budget > 0 and len(thinking) > think_budget
                        and not content and not calls):
                    thinking_cut = True
                    break
        except BaseException:
            stop.set()
            self._abort(holder)
            raise
        finally:
            stop.set()
            reader.join(0.5)
            if reader.is_alive():
                self._abort(holder)
                reader.join(5.0)
        prompt_tokens = usage.get("prompt_tokens") or timings.get("prompt_n") or 0
        eval_tokens = (len(thinking) if thinking_cut
                       else usage.get("completion_tokens") or timings.get("predicted_n") or 0)
        metrics: dict[str, Any] = {"prompt_eval_count": prompt_tokens, "eval_count": eval_tokens}
        if timings.get("prompt_ms") is not None:
            metrics["prompt_eval_duration"] = int(float(timings["prompt_ms"]) * 1e6)
        if timings.get("predicted_ms") is not None:
            metrics["eval_duration"] = int(float(timings["predicted_ms"]) * 1e6)
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached is None:
            cached = timings.get("cache_n")
        if cached is not None:
            metrics["prompt_cached_tokens"] = cached
        self.last_metrics = metrics
        self._progress("done", prompt_tokens=prompt_tokens)
        tool_calls = [{"id": c["id"], "function": {"name": c["name"], "arguments": c["arguments"]}}
                      for _, c in sorted(calls.items())]
        return {
            "message": {"content": "".join(content), "thinking": "".join(thinking),
                        "tool_calls": tool_calls},
            "prompt_eval_count": prompt_tokens,
            "eval_count": eval_tokens,
            "done_reason": "think_budget" if thinking_cut else _FINISH_TO_DONE.get(finish, finish),
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
        think: bool | None = None,
        constrained: bool | None = None,
    ) -> Reply:
        """Same contract as OllamaClient.chat. num_ctx is accepted and not sent
        (a launch flag on these servers). constrained=True drops `tools` and
        asks for tool_call_schema through response_format (R1)."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": temperature,
            "top_p": top_p,
        }
        if self.num_predict > 0:
            payload["max_tokens"] = self.num_predict
        if think is False:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        force_constrained = bool(constrained and tools)
        if tools and not force_constrained:
            payload["tools"] = tools
        constrained = force_constrained or bool(self.constrained_tool_calls and tools)
        if constrained:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "tool_reply", "strict": True,
                                "schema": tool_call_schema(tools or [])}}

        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers=self._headers(),
        )
        data = self._stream(request, 0 if think is False else self.think_budget)

        message = data.get("message", {})
        content = message.get("content", "") or ""
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function", raw)
            args: Any = fn.get("arguments", "")
            if isinstance(args, str):
                if not args.strip():
                    args = {}
                else:
                    try:
                        args = _lenient_json_loads(args)
                    except json.JSONDecodeError:
                        args = {}
            if not isinstance(args, dict):
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
            truncated=data.get("done_reason") == "length",
            thinking_cut=data.get("done_reason") == "think_budget",
        )


def list_models(base_url: str, api_key: str = "", timeout: float = 10.0) -> list[str]:
    """Model ids the server reports at /v1/models."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    request = urllib.request.Request(base_url.rstrip("/") + "/v1/models", headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        body = json.loads(resp.read())
    return [m.get("id", "") for m in body.get("data") or [] if isinstance(m, dict)]


def read_api_key(path: str) -> str:
    """Bearer token from a file (first line). Empty path = no token. A file
    readable by group or others is refused rather than used (secrets live in
    0600 files)."""
    if not path:
        return ""
    key_path = Path(path).expanduser()
    mode = key_path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(
            f"openai_api_key_file {key_path} must be mode 0600 (is {oct(mode & 0o777)})")
    lines = key_path.read_text().strip().splitlines()
    return lines[0].strip() if lines else ""
