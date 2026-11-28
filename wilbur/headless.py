"""Machine-readable output for `wilbur -p --output-format json|stream-json`.

For scripts, cron and CI. stdout carries only JSON; human notices stay on
stderr. `json` prints one object when the run ends; `stream-json` prints one
object per line as the run goes (tool_start, tool_end, assistant) and ends
with the same `result` object.

Exit codes (every output format):
  0    the run finished and the model gave its answer
  1    the run raised an error (backend failure, crash)
  2    bad setup before the run started (e.g. a missing fake-backend script)
  3    stopped at the turn limit before finishing (json/stream-json only;
       text mode keeps exit 0 here, unchanged since before this mode)
  130  interrupted (Ctrl-C / SIGINT)
"""
from __future__ import annotations

import json
import sys
from typing import Any, TextIO

FORMATS = ("text", "json", "stream-json")
EXIT_OK, EXIT_ERROR, EXIT_SETUP, EXIT_TURN_LIMIT, EXIT_INTERRUPTED = 0, 1, 2, 3, 130


class HeadlessRecorder:
    """Collects tool calls and token usage from Agent events; in stream mode
    also writes each event as a JSON line."""

    def __init__(self, fmt: str, out: TextIO | None = None) -> None:
        self.fmt = fmt
        self.out = out or sys.stdout
        self.tool_calls: list[dict[str, Any]] = []
        self.usage = {"prompt_tokens": 0, "output_tokens": 0, "model_calls": 0}
        self.hit_turn_limit = False

    def on_event(self, kind: str, data: dict) -> None:
        if kind == "tool_start":
            self.tool_calls.append({"name": data.get("name"), "args": data.get("args") or {},
                                    "error": None, "seconds": None})
            self._stream({"type": "tool_start", "name": data.get("name"),
                          "args": data.get("args") or {}})
        elif kind == "tool_end":
            for call in reversed(self.tool_calls):
                if call["name"] == data.get("name") and call["error"] is None:
                    call["error"] = bool(data.get("error"))
                    call["seconds"] = round(float(data.get("seconds") or 0.0), 3)
                    break
            self._stream({"type": "tool_end", "name": data.get("name"),
                          "error": bool(data.get("error")),
                          "output": str(data.get("output") or "")})
        elif kind == "assistant":
            self.usage["model_calls"] += 1
            self.usage["prompt_tokens"] += int(data.get("prompt_tokens") or 0)
            self.usage["output_tokens"] += int(data.get("tokens") or 0)
            self._stream({"type": "assistant", "content": data.get("content") or "",
                          "tokens": int(data.get("tokens") or 0)})
        elif kind == "limit":
            self.hit_turn_limit = True
            self._stream({"type": "limit", "turns": data.get("turns")})

    def _stream(self, obj: dict) -> None:
        if self.fmt == "stream-json":
            emit(obj, self.out)

    def finish(self, *, result: str, exit_code: int, session_id: str | None,
               model: str, error: str | None = None) -> int:
        if exit_code == EXIT_OK and self.hit_turn_limit:
            exit_code = EXIT_TURN_LIMIT
        emit({
            "type": "result", "result": result, "is_error": exit_code != EXIT_OK,
            "error": error, "exit_code": exit_code, "session_id": session_id,
            "model": model, "tool_calls": self.tool_calls, "usage": dict(self.usage),
        }, self.out)
        return exit_code


def emit(obj: dict, out: TextIO | None = None) -> None:
    stream = out or sys.stdout
    stream.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    stream.flush()


def setup_error(message: str, out: TextIO | None = None) -> int:
    emit({"type": "result", "result": "", "is_error": True, "error": message,
          "exit_code": EXIT_SETUP, "session_id": None, "model": None,
          "tool_calls": [], "usage": {"prompt_tokens": 0, "output_tokens": 0,
                                      "model_calls": 0}}, out)
    return EXIT_SETUP
