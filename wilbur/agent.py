"""The agent loop: model -> tool calls -> results -> model, until it stops."""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from .config import Config
from .context import CHARS_PER_TOKEN, estimate_tokens, compact
from .fake_backend import make_client
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

Safety rules (the owner's global rules that apply to any coding agent):
- Name exact targets. Never delete, kill, reset or overwrite by pattern, glob or
  "all": no `rm -rf` on a glob, `pkill -f`, `git add -A`/`git add .`,
  `git reset --hard`, `git clean`, `git stash` or `git checkout -- .`. Stage only
  the files you changed, by path.
- Chain dependent steps with `&&`, never `;`, so a later step (commit, push)
  runs only if the checks before it passed. Do not pipe a test run into
  `tail`/`head` when you need its exit code.
- Never print, log or commit a secret value (keys, tokens, passwords, .env
  contents). Refer to where it lives instead.
- Never run sudo, su, doas or pkexec. You have no root access; every attempt
  is refused and raises a security alert. Work as the current user, or report
  that the step needs the owner.
- Do not push, deploy, send mail or spend money unless the user asked for it.
- A check that could not run is not a pass. Say which a result is: verified
  (you ran it just now), unknown (could not check, and why), or assumed.
- When a version-carrying file changes, bump the version and every place that
  shows it in the same change.

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
        self.last_reply_empty = False
        self.turn_test_runs = 0
        # OllamaClient is looked up here, at call time, so a test that patches
        # wilbur.agent.OllamaClient still takes effect; `backend: fake` in the
        # config selects the scripted client instead (wilbur.fake_backend).
        self.client = make_client(config, model or config.model, ollama_cls=OllamaClient)
        registry = build_registry(include_task=include_task)
        if getattr(config, "image_generation", False):
            from .tools.image import GenerateImage
            registry["generate_image"] = GenerateImage()
        if tool_names is not None:
            registry = {k: v for k, v in registry.items() if k in tool_names}
        self.registry = registry
        self.schema = to_schema(registry)
        # The tool schema is sent with every call and counts against num_ctx.
        self._schema_tokens = int(len(json.dumps(self.schema, default=str)) / CHARS_PER_TOKEN)
        # The real prompt size Ollama reported for the last call, and our own
        # estimate of what was sent then. The chars/token estimate runs far
        # low on high-entropy tool output (2026-10-17: estimate 45,004 vs a
        # real 64,582 of 65,536, so it never compacted and the model ran out
        # of room and returned empty replies). 0 = no real figure yet.
        self._last_prompt_tokens = 0
        self._estimate_at_last_prompt = 0
        self.known = set(registry)
        self.state = state if state is not None else RunState()
        self.ctx = ToolContext(cwd=cwd, config=config, read_files={}, approve=approve)
        self.ctx.state = self.state
        self.ctx.cancel = self.cancel_event
        # wilbur.inbox.Inbox (set by the REPL): a message typed mid-turn is
        # read between rounds instead of only when the whole turn ends.
        self.inbox = None
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
        empty_retries = 0
        # True when the one empty-reply retry ALSO came back empty: keep-going
        # stops with reason `empty_reply` instead of whatever check fires next
        # (2026-10-16: a run stopped after two empty replies and the log said
        # "horizon reached", hiding the real cause).
        self.last_reply_empty = False
        # One compact-and-retry per run for an empty reply that came back with
        # the window full: a context-full reply is not an empty reply.
        context_full_retried = False
        # Test commands run in this call (the model's or the harness's own):
        # the claims guard (wilbur.doneclaim) reads it.
        self.turn_test_runs = 0
        # Lines the user typed that were read mid-turn (wilbur.inbox): keep-going
        # treats them like a line still queued, so they win over the nudge.
        self.turn_messages_injected = 0

        for turn in range(self.config.max_turns):
            if self.cancel_event.is_set():
                final = "Cancelled by user."
                self.on_event("cancelled", {})
                break
            if turn and self._check_messages():
                final = "Paused: a command is waiting."
                break
            self._maybe_compact()
            self._refresh_briefing()
            sent_estimate = self._estimate_prompt()
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
            if reply.prompt_tokens:
                self._last_prompt_tokens = reply.prompt_tokens
                self._estimate_at_last_prompt = sent_estimate
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

            if (not reply.tool_calls and not (reply.content or "").strip()
                    and not context_full_retried
                    and reply.prompt_tokens >= self.config.num_ctx - self.config.reserve_output):
                # The window was full: the model had no room left to answer
                # and spent it thinking. Compact and ask again rather than
                # counting it toward the empty-reply stop.
                context_full_retried = True
                self.messages.pop()
                self._maybe_compact(force=True, reason="context_full")
                continue

            if not reply.tool_calls:
                # Stall (b), diagnosed 2026-10-02: a thinking-capable local
                # model (qwen3) sometimes returns a message with everything in
                # `thinking` and an EMPTY `content` and no tool calls. The old
                # loop broke here with final="" -- a turn that ended with no
                # reply at all and nothing for keep-going to act on, so the
                # session just went silent. Nudge once for a concrete answer or
                # next action before giving up; if it is still empty, surface
                # the thinking rather than nothing.
                if not (reply.content or "").strip() and empty_retries < 1:
                    empty_retries += 1
                    self.on_event("empty_reply", {"had_thinking": bool(reply.thinking)})
                    self.messages.append({
                        "role": "user",
                        "content": ("Your last reply was empty. Either take the next "
                                    "concrete action with a tool, or state plainly what "
                                    "you did, what is left, and the next step. Do not "
                                    "reply with an empty message."),
                    })
                    continue
                if not (reply.content or "").strip() and empty_retries >= 1:
                    self.last_reply_empty = True
                final = reply.content or (reply.thinking or "").strip()
                break

            empty_retries = 0
            cancelled_mid_round = False
            for call in reply.tool_calls:
                if self.cancel_event.is_set():
                    cancelled_mid_round = True
                    break
                result = self._execute(call)
                self.messages.append(self._tool_message(call, result))
                self.on_event("message", {})
            # Remaining calls of this round still run (a skipped call would
            # leave the transcript with a tool call and no result); the
            # message is injected right after this round instead.
            if cancelled_mid_round:
                final = "Cancelled by user."
                self.on_event("cancelled", {})
                break
        else:
            final = self._limit_summary()
            self.on_event("limit", {"turns": self.config.max_turns})

        return final

    # ------------------------------------------------------------------ #

    def _check_messages(self) -> bool:
        """Read a message typed mid-turn at this safe point (between tool
        rounds). Plain lines go into the conversation now; True means a
        slash command or EOF is waiting and the turn must end so the REPL
        handles it."""
        inbox = self.inbox
        if inbox is None or not inbox.pending():
            return False
        from .inbox import injection_text
        lines = inbox.take_injectable()
        if lines:
            self.messages.append({"role": "user", "content": injection_text(lines)})
            self.turn_messages_injected = getattr(self, "turn_messages_injected", 0) + len(lines)
            self.on_event("message_injected", {"lines": lines})
            # A slash command queued behind the lines ends the turn now (the
            # lines stay in the conversation for the next turn); waiting one
            # more model call for it could break the message_check_max_s bound.
        return inbox.pending()

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
        if passed is not None:
            self.turn_test_runs = getattr(self, "turn_test_runs", 0) + 1
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

        if call.name == "run_tests":
            self.turn_test_runs = getattr(self, "turn_test_runs", 0) + 1
        elif call.name == "run_bash":
            from .doneclaim import is_test_command
            if is_test_command(str((call.arguments or {}).get("command", ""))):
                self.turn_test_runs = getattr(self, "turn_test_runs", 0) + 1

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

        # Progress signals for the stuck detector (wilbur.horizon).
        self.state.last_tool = f"{call.name}({self._arg_signature(call)})"
        if call.name in WRITE_TOOLS:
            self.state.writes += 1

        # A successful write is the moment to check the project still builds.
        if call.name in WRITE_TOOLS:
            output = result.output
            if getattr(self.config, "critic_pass", False):
                from .critic import review
                note = review(self.client, self.config, call.name,
                              call.arguments or {}, result.display or result.output)
                if note:
                    self.on_event("critic", {"name": call.name, "note": note})
                    output += "\n\n" + note
            line = self._verify_after_write()
            if line:
                return output + "\n\n[harness ran the project's own check]\n" + line
            return output
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

    def _estimate_prompt(self) -> int:
        """Estimated prompt size: the messages plus the tool schema."""
        return estimate_tokens(self.messages) + self._schema_tokens

    def _prompt_size(self) -> int:
        """Best figure for the next prompt: the larger of our estimate and the
        real size Ollama last reported plus what has been added since."""
        estimate = self._estimate_prompt()
        if not self._last_prompt_tokens:
            return estimate
        added = max(0, estimate - self._estimate_at_last_prompt)
        return max(estimate, self._last_prompt_tokens + added)

    def _maybe_compact(self, force: bool = False, reason: str = "budget") -> None:
        budget = int(self.config.num_ctx * self.config.compact_at) - self.config.reserve_output
        used = self._prompt_size()
        if used < budget and not force:
            return
        before = len(self.messages)
        self.messages = compact(self.messages, self.client, self.config)
        # The last real figure describes the old history; start over.
        self._last_prompt_tokens = 0
        self._estimate_at_last_prompt = 0
        self.on_event("compact", {
            "before_messages": before, "after_messages": len(self.messages),
            "before_tokens": used, "after_tokens": self._estimate_prompt(),
            "reason": reason,
        })
