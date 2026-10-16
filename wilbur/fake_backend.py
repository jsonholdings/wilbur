"""Deterministic fake model backend: scripted replies, no GPU, no Ollama, no network.

`FakeClient` implements the same `chat()` interface as `wilbur.llm.OllamaClient`
and returns a queue of scripted `Reply` objects in order, recording every
request it was sent. It exists so the whole test suite -- and a scripted
end-to-end run of the real CLI -- can drive the agent loop with no model.

It is never chosen silently. Two ways in, both explicit:

- a test fixture builds one (`tests/conftest.py`) and patches it in;
- configuration: `"backend": "fake"` plus `"fake_script": "<file.json>"` in
  config.json, or `wilbur --backend fake --fake-script <file.json>`. The CLI
  prints a warning on every run that uses it, an unknown backend name is an
  error, and `backend: fake` without a script is an error -- an unreachable
  Ollama never falls back to the fake.

Script file format (JSON): a list of turns, each either a string (plain
content, no tool call) or an object

    {"content": "...", "tool_calls": [{"name": "read_file",
                                       "arguments": {"path": "a.txt"}}],
     "thinking": "..."}

or an object `{"replies": [...], "exhausted": "..."}` to also set the reply
returned once the script runs out. A string or an object with no
`tool_calls` goes through the same `recover_tool_calls()` step the real
client applies to model output, so a script can reproduce a model that
writes its tool call as text.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable

from .llm import OllamaClient, Reply, ToolCall, recover_tool_calls

BACKENDS = ("ollama", "fake")
DEFAULT_EXHAUSTED = "done"


class FakeBackendError(ValueError):
    """The fake backend was selected but cannot be used as configured."""


def _parse_turn(raw: Any) -> dict[str, Any]:
    """Validate one script entry into a dict of Reply fields plus a flag
    saying whether the real client's text recovery should run over it."""
    if isinstance(raw, str):
        return {"content": raw, "tool_calls": [], "recover": True}
    if not isinstance(raw, dict):
        raise FakeBackendError(f"script turn must be a string or object, got {type(raw).__name__}")
    calls = []
    for c in raw.get("tool_calls") or []:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str):
            raise FakeBackendError(f"tool call needs a string 'name': {c!r}")
        args = c.get("arguments", {})
        if not isinstance(args, dict):
            raise FakeBackendError(f"tool call 'arguments' must be an object: {c!r}")
        calls.append(ToolCall(name=c["name"], arguments=args, call_id=c.get("id", "")))
    for field in ("content", "thinking"):
        if raw.get(field) is not None and not isinstance(raw[field], str):
            raise FakeBackendError(f"script turn {field!r} must be a string: {raw!r}")
    return {
        "content": raw.get("content", "") or "",
        "tool_calls": calls,
        "thinking": raw.get("thinking", "") or "",
        "recover": not calls,
    }


class FakeClient:
    """Returns queued replies, in order, and records every request it saw.

    `replies` holds `Reply` objects (returned exactly as given) and/or raw
    script entries (strings or dicts, see the module docstring). Once the
    queue is empty every further call returns `exhausted` as plain content,
    which ends the agent's turn.
    """

    def __init__(self, replies: Iterable[Any] = (), *, exhausted: str = DEFAULT_EXHAUSTED):
        self.replies = list(replies)
        self.exhausted = exhausted
        self.seen: list[list[dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, *a: Any, **kw: Any) -> "FakeClient":
        # Lets a test patch an instance in where the `OllamaClient` class is
        # looked up: `OllamaClient(base_url, model, timeout)` returns it.
        return self

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        known_tools: set[str] | None = None,
        **options: Any,
    ) -> Reply:
        with self._lock:
            self.seen.append([dict(m) for m in messages])
            self.requests.append({"tools": tools, "known_tools": known_tools, "options": options})
            raw = self.replies.pop(0) if self.replies else self.exhausted
        if isinstance(raw, Reply):
            return raw
        turn = _parse_turn(raw)
        content, calls = turn["content"], turn["tool_calls"]
        if turn["recover"] and known_tools:
            calls, content = recover_tool_calls(content, known_tools)
        return Reply(content=content, tool_calls=calls, thinking=turn.get("thinking", ""))

    @classmethod
    def from_script(cls, path: str | Path) -> "FakeClient":
        p = Path(path).expanduser()
        try:
            data = json.loads(p.read_text())
        except FileNotFoundError:
            raise FakeBackendError(f"fake backend script not found: {p}") from None
        except json.JSONDecodeError as exc:
            raise FakeBackendError(f"fake backend script {p} is not valid JSON: {exc}") from None
        exhausted = DEFAULT_EXHAUSTED
        if isinstance(data, dict):
            exhausted = data.get("exhausted", DEFAULT_EXHAUSTED)
            data = data.get("replies")
        if not isinstance(data, list):
            raise FakeBackendError(f"fake backend script {p} must be a list of turns "
                                   "or an object with a 'replies' list")
        for turn in data + [exhausted]:
            _parse_turn(turn)  # fail on load, not halfway through a run
        return cls(data, exhausted=exhausted)


# One queue per script file per process: a parent agent, its `task`
# subagents and team-mode roles each build their own Agent, and must all read
# the same script in order rather than each restarting it from the top.
_shared: dict[str, FakeClient] = {}
_shared_lock = threading.Lock()


def shared_client(path: str | Path) -> FakeClient:
    key = str(Path(path).expanduser().resolve())
    with _shared_lock:
        if key not in _shared:
            _shared[key] = FakeClient.from_script(key)
        return _shared[key]


def reset_shared_clients() -> None:
    with _shared_lock:
        _shared.clear()


def check_backend(config: Any) -> str:
    """Return the configured backend name, or raise if it is unusable."""
    backend = getattr(config, "backend", "ollama") or "ollama"
    if backend not in BACKENDS:
        raise FakeBackendError(f"unknown backend {backend!r} (expected one of {', '.join(BACKENDS)})")
    if backend == "fake" and not getattr(config, "fake_script", ""):
        raise FakeBackendError("backend 'fake' needs a script: set fake_script in config.json "
                               "or pass --fake-script FILE")
    return backend


def make_client(config: Any, model: str, ollama_cls: Any = OllamaClient) -> Any:
    """Build the model client `config` selects. `ollama_cls` is passed by the
    caller so tests that patch `wilbur.agent.OllamaClient` keep working."""
    if check_backend(config) == "fake":
        return shared_client(config.fake_script)
    client = ollama_cls(config.base_url, model, config.request_timeout)
    if getattr(config, "constrained_tool_calls", False):
        client.constrained_tool_calls = True
    return client
