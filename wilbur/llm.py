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
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
    # True when the server stopped the reply at the output cap (num_predict,
    # done_reason "length"): the reply is cut off, not finished.
    truncated: bool = False
    # True when the harness cut the stream because the reasoning ran past
    # OllamaClient.think_budget with no answer or tool call started.
    thinking_cut: bool = False


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


_GO_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_GO_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def keep_alive_seconds(keep_alive: str) -> float | None:
    """Seconds for an Ollama keep_alive value ("30m", "1h30m", "300", "300s").
    None when it cannot bound an expiry: empty (server default unknown here),
    negative (keep forever), zero, or unparseable."""
    text = (keep_alive or "").strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        if not re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ms|h|m|s))+", text):
            return None
        seconds = sum(float(n) * _GO_UNIT_SECONDS[u] for n, u in _GO_DURATION_RE.findall(text))
    return seconds if seconds > 0 else None


def _parse_ollama_time(text: str) -> datetime | None:
    """Ollama's RFC 3339 times carry nanoseconds, which fromisoformat rejects;
    keep six fractional digits."""
    if not text:
        return None
    trimmed = re.sub(r"(\.\d{6})\d+", r"\1", text).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(trimmed)
    except ValueError:
        return None


class ServerActivity:
    """Answers "is the model server still generating?" when a stream has gone
    quiet past the stall window.

    Measured 2026-10-21 (Ollama 0.40.2, qwen3.8 27b): while the model writes a
    tool call, Ollama buffers it and sends NO bytes until the call is complete
    -- Wilbur's socket sat at lastrcv 109 s while the server's n_gen climbed
    from 7,215 to 9,489 tokens. Before 2.41.1 that silence raised "model server
    not responding" at 120 s and cancelled a healthy generation (22:14, 22:16).

    Two signals, both describing the SERVER, so they work through the
    contrib/remote-gpu tunnel too:

    - /api/ps: Ollama refreshes a model's expires_at to (finish + keep_alive)
      when a request ends, and leaves it alone while one runs. If it was last
      set before our silence began, nothing finished since: still working.
      Server time comes from the response's Date header, so a laptop's clock
      skew does not matter.
    - local nvidia-smi utilisation, only when the server is on this machine
      (loopback URL) and /api/ps could not decide (keep_alive unknown or -1).

    check(silent_s) returns True (working), False (stopped), or None (could
    not tell -- the caller keeps the old raise-at-the-window behaviour)."""

    GPU_BUSY_PERCENT = 20
    SLACK_S = 5.0

    def __init__(self, base_url: str, model: str, keep_alive: str, mode: str = "auto",
                 *, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.keep_alive_s = keep_alive_seconds(keep_alive)
        self.mode = mode
        self.timeout = timeout

    def check(self, silent_s: float) -> bool | None:
        verdict: bool | None = None
        if self.mode in ("auto", "ps"):
            verdict = self._from_ps(silent_s)
        if verdict is None and self.mode in ("auto", "gpu"):
            verdict = self._from_gpu()
        return verdict

    def _from_ps(self, silent_s: float) -> bool | None:
        try:
            with urllib.request.urlopen(self.base_url + "/api/ps", timeout=self.timeout) as resp:
                body = json.loads(resp.read())
                date_header = resp.headers.get("Date") if resp.headers else None
        except Exception:  # noqa: BLE001 -- an unreachable probe is "cannot tell"
            return None
        models = body.get("models") if isinstance(body, dict) else None
        if not isinstance(models, list):
            return None
        entry = next((m for m in models if isinstance(m, dict)
                      and self.model in (m.get("name"), m.get("model"))), None)
        if entry is None:
            return False  # not even loaded: nothing is generating for us
        if self.keep_alive_s is None:
            return None
        expires = _parse_ollama_time(str(entry.get("expires_at") or ""))
        if expires is None:
            return None
        try:
            server_now = parsedate_to_datetime(date_header) if date_header else None
        except (TypeError, ValueError):
            server_now = None
        if server_now is None:
            server_now = datetime.now(timezone.utc)
        since_set = (server_now - expires).total_seconds() + self.keep_alive_s
        return since_set >= silent_s - self.SLACK_S

    def _from_gpu(self) -> bool | None:
        host = urllib.parse.urlparse(self.base_url).hostname or ""
        if host not in ("127.0.0.1", "localhost", "::1") or not shutil.which("nvidia-smi"):
            return None
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=self.timeout, check=True).stdout
            busiest = max(int(v) for v in out.split() if v.strip().isdigit())
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        return busiest >= self.GPU_BUSY_PERCENT


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
        # When a stall window passes, busy_check(silent_s) is asked whether the
        # server is still generating (ServerActivity.check; None = no probe,
        # the pre-2.41.1 behaviour). While it says True the wait goes on, up to
        # stall_busy_max_s of continuous silence. Config.stall_busy_check,
        # Config.stall_busy_max_s.
        self.busy_check: Any = None
        self.stall_busy_max_s: float = 600.0
        # Output cap per reply (Ollama num_predict; 0 = none). A reply that hits
        # it comes back with Reply.truncated. Config.num_predict.
        self.num_predict: int = 0
        # Reasoning cap per reply, in streamed thinking chunks (~1 token each;
        # 0 = none). A reply still thinking past it, with no content or tool
        # call started, is cut and comes back with Reply.thinking_cut.
        # Config.think_budget_tokens.
        self.think_budget: int = 0
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

    def _open(self, request: urllib.request.Request, read_timeout: float) -> Any:
        """Connect, retrying only transient failures with backoff. A timeout is
        a stall, never retried."""
        attempt = 0
        while True:
            try:
                return urllib.request.urlopen(request, timeout=read_timeout)
            except Exception as exc:  # noqa: BLE001 -- classified below
                if _is_timeout(exc) or not _is_transient(exc) or attempt >= self.max_retries:
                    raise
                self._sleep(self.backoff_base * (2 ** attempt))
                attempt += 1

    def _read_lines(self, request: urllib.request.Request, read_timeout: float,
                    out: "queue.Queue[tuple[str, Any]]", stop: threading.Event,
                    holder: dict[str, Any]) -> None:
        """Reader thread: open the stream and hand each raw line to `out`.
        A blocking read never has to be resumed after a socket timeout (which a
        timed-out socket file refuses), so the stall decision lives in the
        caller's queue wait, not here."""
        try:
            response = self._open(request, read_timeout)
        except BaseException as exc:  # noqa: BLE001 -- re-raised by the caller
            out.put(("error", exc))
            return
        with holder["lock"]:
            holder["response"] = response
        try:
            while not stop.is_set():
                line = response.readline()
                out.put(("line", line))
                if not line:
                    return
        except BaseException as exc:  # noqa: BLE001 -- re-raised by the caller
            out.put(("error", exc))
        finally:
            # Closed under the lock _abort takes, so _abort never shuts down a
            # descriptor number the OS has already handed to another socket.
            with holder["lock"]:
                holder["response"] = None
                try:
                    response.close()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _abort(holder: dict[str, Any]) -> None:
        """Unblock the reader thread: shutting the connection down makes a
        blocked read return EOF. Works on a duplicate of the fd, so nothing
        here touches a private attribute."""
        with holder["lock"]:
            OllamaClient._abort_locked(holder.get("response"))

    @staticmethod
    def _abort_locked(response: Any) -> None:
        if response is None:
            return
        try:
            fd = response.fileno()
            dup = socket.socket(fileno=os.dup(fd))
            try:
                dup.shutdown(socket.SHUT_RDWR)
            finally:
                dup.close()
        except Exception:  # noqa: BLE001 -- a fake or closed response
            close = getattr(response, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

    def _still_working(self, silent: float) -> bool:
        if self.busy_check is None or silent >= self.stall_busy_max_s:
            return False
        try:
            return self.busy_check(silent) is True
        except Exception:  # noqa: BLE001 -- a broken probe must not hide a stall
            return False

    def _stream(self, request: urllib.request.Request, think_budget: int = 0) -> dict[str, Any]:
        """POST a streaming chat and fold the NDJSON chunks into one
        non-streamed-shaped response.

        Every silence -- the wait for the first byte (model load + prompt
        reading) and each gap between chunks -- is bounded by the stall window.
        When it passes, busy_check asks the server whether it is still
        generating (Ollama sends nothing while it writes a tool call); while it
        is, the wait goes on with a "busy" progress event, up to
        stall_busy_max_s. Only a server that has really stopped raises
        ModelNotResponding. A failure to connect (or a 5xx) is retried with
        backoff.

        think_budget > 0 cuts the stream once that many thinking chunks have
        arrived with no content or tool call yet: closing the connection makes
        Ollama stop generating, and done_reason comes back "think_budget"."""
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
        tool_calls: list[dict[str, Any]] = []
        final: dict[str, Any] = {}
        thinking_cut = False
        phase = ""
        started = self._clock()
        last_byte = started
        try:
            while True:
                try:
                    kind, item = out.get(timeout=self.stall_timeout)
                except queue.Empty:
                    silent = self._clock() - last_byte
                    over_total = bool(self.timeout) and self._clock() - started > self.timeout
                    if over_total or not self._still_working(silent):
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
                if (think_budget > 0 and len(thinking) > think_budget
                        and not content and not tool_calls):
                    thinking_cut = True
                    break
                if self.timeout and self._clock() - started > self.timeout:
                    raise ModelNotResponding(self.timeout)
        except BaseException:
            stop.set()
            self._abort(holder)
            raise
        finally:
            stop.set()
            reader.join(0.5)  # a real server closes the stream right after done
            if reader.is_alive():
                self._abort(holder)
                reader.join(5.0)
        self._progress("done", prompt_tokens=final.get("prompt_eval_count") or 0)
        return {
            "message": {"content": "".join(content), "thinking": "".join(thinking),
                        "tool_calls": tool_calls},
            "prompt_eval_count": final.get("prompt_eval_count") or 0,
            "eval_count": (len(thinking) if thinking_cut else final.get("eval_count") or 0),
            "done_reason": "think_budget" if thinking_cut else final.get("done_reason") or "",
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
    ) -> Reply:
        """think=False asks the server for no reasoning at all (Ollama `think`);
        the default leaves the model's own setting and applies think_budget."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature, "top_p": top_p, "num_ctx": num_ctx},
        }
        if self.num_predict > 0:
            payload["options"]["num_predict"] = self.num_predict
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive
        if think is False:
            payload["think"] = False
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
        data = self._stream(request, 0 if think is False else self.think_budget)

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
            truncated=data.get("done_reason") == "length",
            thinking_cut=data.get("done_reason") == "think_budget",
        )
