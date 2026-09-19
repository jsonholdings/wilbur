"""The agent loop: model -> tool calls -> results -> model, until it stops."""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from .config import Config
from .context import estimate_tokens, compact
from .llm import OllamaClient, ToolCall
from .state import BRIEFING_MARKER, RunState
from .tools import ToolContext, build_registry, to_schema
from .tools.base import Approval

# Tools whose success changes files on disk, and therefore the moment at
# which the project's own check is worth running.
WRITE_TOOLS = {"write_file", "edit_file"}

SYSTEM_PROMPT = """You are Wilbur, a coding agent working in a terminal on the user's machine.

You act rather than advise. When the user asks for something, use your tools to
do it: read the real files, run the real commands, make the real edits. Never
guess at a file's contents when you can read it, and never claim a change works
until a command you ran says so.

Working rules:
- Read a file before you edit or overwrite it.
- Prefer edit_file over write_file for changes to existing files.
- Use grep and glob to find code. Do not guess at paths.
- Run one tool at a time and read its result before deciding the next step.
- For any job with more than two steps, call todo_write first and keep it current.
- Break a large job down before starting it. Write the plan with todo_write, then
  work one item at a time and mark each completed before moving on.
- When a plan item is self-contained and would take many tool calls -- a search
  across the codebase, a mechanical change over several files -- delegate it with
  the task tool instead of doing it inline. You keep the plan; the subagent
  spends the context.
- When a command fails, read the error and fix the cause. Do not retry unchanged.
- Match the style, naming, and structure of the surrounding code.
- Split a long task into small, numbered, independently-testable chunks before
  starting; verify each chunk with a real command before moving to the next,
  rather than attempting the whole task in one unbroken run.

Be concise. Report what you did and what it means, not how you went about it.
When you have finished the task, say so plainly and stop calling tools."""

# Asked with no tools, once, when the turn budget runs out. Kept off the
# transcript (see `_limit_summary`) so a `/continue` sees the same context it
# would have seen had the limit not been hit at all.
LIMIT_SUMMARY_PROMPT = """You have used up the tool-round budget for this task before
finishing. Without calling any tools, answer in three short parts:

1. What you did.
2. What is left.
3. The next concrete step.

Keep it brief."""


class Agent:
    def __init__(
        self,
        config: Config,
        cwd: str,
        *,
        approve: Callable[[str, str], Approval],
        on_event: Callable[[str, dict], None] | None = None,
        model: str | None = None,
        tool_names: list[str] | None = None,
        include_task: bool = True,
        system_extra: str = "",
        state: RunState | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.cwd = cwd
        self.on_event = on_event or (lambda kind, data: None)
        # Cooperative cancellation: a subagent runs on its own daemon thread
        # (see tools/task.py), so a Ctrl-C on the main thread never reaches
        # it as a KeyboardInterrupt. Checking this flag between rounds and
        # before each tool call is what actually stops it -- setting it is
        # the caller's job (Task.run's KeyboardInterrupt handler).
        self.cancel_event = cancel if cancel is not None else threading.Event()
        self.client = OllamaClient(config.base_url, model or config.model,
                                   config.request_timeout)
        registry = build_registry(include_task=include_task)
        if tool_names is not None:
            registry = {k: v for k, v in registry.items() if k in tool_names}
        self.registry = registry
        self.schema = to_schema(registry)
        self.known = set(registry)
        self.state = state if state is not None else RunState()
        self.ctx = ToolContext(cwd=cwd, config=config, read_files={}, approve=approve)
        self.ctx.state = self.state
        self.ctx.cancel = self.cancel_event
        system = SYSTEM_PROMPT + (f"\n\n{system_extra}" if system_extra else "")
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

    # ------------------------------------------------------------------ #

    def run(self, user_input: str) -> str:
        # The objective is captured verbatim once and never rewritten. Later
        # turns re-read it from state, not from a compacted transcript.
        if not self.state.objective:
            self.state.objective = user_input
        self.messages.append({"role": "user", "content": user_input})
        final = ""

        for turn in range(self.config.max_turns):
            if self.cancel_event.is_set():
                final = "Cancelled by user."
                self.on_event("cancelled", {})
                break
            self._maybe_compact()
            self._refresh_briefing()
            started = time.time()
            reply = self.client.chat(
                self.messages,
                self.schema,
                known_tools=self.known,
                num_ctx=self.config.num_ctx,
                temperature=self.config.temperature,
                top_p=self.config.top_p,
            )
            elapsed = time.time() - started
            self.on_event("assistant", {
                "content": reply.content, "thinking": reply.thinking,
                "tokens": reply.eval_tokens, "seconds": elapsed,
                "prompt_tokens": reply.prompt_tokens,
            })

            assistant: dict[str, Any] = {"role": "assistant", "content": reply.content}
            if reply.tool_calls:
                assistant["tool_calls"] = [
                    self._call_message(c) for c in reply.tool_calls
                ]
            self.messages.append(assistant)
            self.on_event("message", {})

            if not reply.tool_calls:
                final = reply.content
                break

            cancelled_mid_round = False
            for call in reply.tool_calls:
                if self.cancel_event.is_set():
                    cancelled_mid_round = True
                    break
                result = self._execute(call)
                self.messages.append(self._tool_message(call, result))
                self.on_event("message", {})
            if cancelled_mid_round:
                final = "Cancelled by user."
                self.on_event("cancelled", {})
                break
        else:
            final = self._limit_summary()
            self.on_event("limit", {"turns": self.config.max_turns})

        return final

    # ------------------------------------------------------------------ #

    def _limit_summary(self) -> str:
        """Ask the model, once and without tools, what it did, what is left,
        and what to do next -- in place of the bare "stopped" message.

        Built on a *copy* of the transcript and never appended to
        `self.messages`: the real transcript ends on the last tool result,
        exactly as if the limit had not been hit, so `/continue` (or typing
        "continue") resumes with a fresh round budget and unaltered context
        rather than a transcript polluted by a call the harness made, not the
        user.
        """
        header = f"Stopped after {self.config.max_turns} tool rounds without finishing."
        probe = self.messages + [{"role": "user", "content": LIMIT_SUMMARY_PROMPT}]
        try:
            reply = self.client.chat(
                probe, None, known_tools=set(),
                num_ctx=self.config.num_ctx,
                temperature=self.config.temperature,
                top_p=self.config.top_p,
            )
            summary = reply.content.strip()
        except Exception:
            summary = ""

        tail = 'Type "/continue" (or just "continue") to resume with a fresh round budget.'
        if summary:
            return f"{header}\n\n{summary}\n\n{tail}"
        return f"{header} The task may need to be broken into smaller steps.\n\n{tail}"

    def _refresh_briefing(self) -> None:
        """Keep exactly one current briefing pinned directly after the system
        prompt.

        Replaced rather than appended: a stale copy further down the transcript
        would contradict the live one, and the model has no way to tell which
        is current.
        """
        text = self.state.briefing()
        head = 1 if self.messages and self.messages[0]["role"] == "system" else 0
        self.messages = (
            self.messages[:head]
            + [m for m in self.messages[head:]
               if not (m.get("role") == "system"
                       and BRIEFING_MARKER in (m.get("content") or ""))]
        )
        if text:
            self.messages.insert(head, {"role": "system", "content": text})

    def _verify_after_write(self) -> str | None:
        """Run the project's own check and record the verdict.

        Returns a line to append to the tool result, or None when the project
        defines no check -- in which case we say nothing rather than inventing
        a command to run against someone's repository.
        """
        from . import verify
        check = verify.detect(self.cwd)
        if check is None:
            return None
        passed, output = verify.run(check, self.cwd)
        line = verify.summarise(check, passed, output)
        self.state.record_verification(line.splitlines()[0])
        if passed is False:
            self.state.record_failure(f"edit verified by {check.label}",
                                      "the check failed after this change")
        self.on_event("verify", {"label": check.label, "passed": passed})
        return line

    # ------------------------------------------------------------------ #

    @staticmethod
    def _call_message(call: ToolCall) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "function": {"name": call.name, "arguments": call.arguments}
        }
        if call.call_id:
            msg["id"] = call.call_id
        return msg

    @staticmethod
    def _tool_message(call: ToolCall, result: str) -> dict[str, Any]:
        """A tool result the model can match to the call that produced it.

        Ollama takes `tool_name` on a tool message (verified against 0.33.3).
        Without it a turn containing more than one tool call comes back as an
        unlabelled list and the model has to infer the pairing from order --
        the loop does iterate over multiple calls, so this is reachable.
        """
        msg: dict[str, Any] = {
            "role": "tool", "tool_name": call.name, "content": result,
        }
        if call.call_id:
            msg["tool_call_id"] = call.call_id
        return msg

    def _execute(self, call: ToolCall) -> str:
        tool = self.registry.get(call.name)
        if tool is None:
            available = ", ".join(sorted(self.known))
            self.on_event("tool_error", {"name": call.name})
            return f"No tool named {call.name!r}. Available tools: {available}"

        self.on_event("tool_start", {
            "name": call.name, "args": call.arguments, "recovered": call.recovered,
        })
        started = time.time()
        try:
            result = tool.run(call.arguments, self.ctx)
        except KeyError as exc:
            result_text = (f"Missing required argument {exc} for {call.name}. "
                           f"Schema: {json.dumps(tool.schema)}")
            self.on_event("tool_end", {"name": call.name, "error": True,
                                       "output": result_text,
                                       "seconds": time.time() - started})
            return result_text
        except Exception as exc:  # a tool crash must not kill the session
            result_text = f"{call.name} raised {type(exc).__name__}: {exc}"
            self.on_event("tool_end", {"name": call.name, "error": True,
                                       "output": result_text,
                                       "seconds": time.time() - started})
            return result_text

        self.on_event("tool_end", {
            "name": call.name, "error": result.is_error,
            "output": result.display or result.output,
            "seconds": time.time() - started,
        })

        # A tool that failed is a dead end worth remembering. Local models will
        # otherwise reissue the identical call; the ledger lets the next
        # briefing say so instead of watching it happen again.
        if result.is_error:
            signature = f"{call.name}({self._arg_signature(call)})"
            self.state.record_failure(signature, result.output)
            n = self.state.repeated(signature)
            if n > 1:
                return (result.output +
                        f"\n\n[harness] This is attempt {n} at the same call and it has "
                        f"failed every time. Change the approach rather than retrying.")
            return result.output

        # A successful write is the moment to check the project still builds.
        if call.name in WRITE_TOOLS:
            line = self._verify_after_write()
            if line:
                return result.output + "\n\n[harness ran the project's own check]\n" + line
        return result.output

    @staticmethod
    def _arg_signature(call: ToolCall) -> str:
        """Enough of the arguments to tell two calls apart, short enough to
        keep the ledger readable."""
        args = call.arguments or {}
        for key in ("command", "file_path", "path", "pattern", "prompt"):
            if key in args:
                return f"{key}={str(args[key])[:80]}"
        return ",".join(sorted(args))[:80]

    def _maybe_compact(self) -> None:
        budget = int(self.config.num_ctx * self.config.compact_at) - self.config.reserve_output
        used = estimate_tokens(self.messages)
        if used < budget:
            return
        before = len(self.messages)
        self.messages = compact(self.messages, self.client, self.config)
        self.on_event("compact", {
            "before_messages": before, "after_messages": len(self.messages),
            "before_tokens": used, "after_tokens": estimate_tokens(self.messages),
        })
