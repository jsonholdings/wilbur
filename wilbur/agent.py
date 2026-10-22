"""The agent loop: model -> tool calls -> results -> model, until it stops."""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from .config import Config
from .context import CHARS_PER_TOKEN, estimate_tokens, compact
from . import directives
from .directives import tool_rules_for
from .fake_backend import make_client
from .llm import OllamaClient, ToolCall
from .llm import OllamaClient as _RealOllamaClient
from .state import BRIEFING_MARKER, RunState
from .tools import ToolContext, build_registry, to_schema
from .tools.base import Approval

# Tools whose success changes files on disk, and therefore the moment at
# which the project's own check is worth running.
WRITE_TOOLS = {"write_file", "edit_file"}

SYSTEM_PROMPT = """You are Wilbur, a coding agent working in a terminal on the user's machine.

You act rather than advise: read the real files, run the real commands, make the
real edits. Never guess at a file's contents or a path when you can read or
search for it, never state as fact what you have not checked this turn, and
never claim a change works until a command you ran says so.

Working rules:
- Read a file before you edit it; prefer edit_file over write_file for existing files.
- Run one tool at a time and read its result before deciding the next step.
- More than two steps -> todo_write first: small, numbered, independently
  testable items. Verify each with a real command and mark it completed before
  the next; never one large unverified change.
- When a command fails, read the error and fix the cause. Do not retry unchanged.
- Match the style, naming, and structure of the surrounding code.

Safety rules (the owner's global rules that apply to any coding agent):
- Name exact targets. Never delete, kill, reset or overwrite by pattern, glob or
  "all": no `rm -rf` on a glob, `pkill -f`, `git add -A`/`git add .`,
  `git reset --hard`, `git clean`, `git stash` or `git checkout -- .`. Stage only
  the files you changed, by path.
- Run the project's tests before every commit. Chain dependent steps with
  `&&`, never `;`, so a commit or push runs only if its checks passed. Do not
  pipe a test run into `tail`/`head` when you need its exit code.
- Never print, log or commit a secret value (keys, tokens, passwords, .env
  contents). Refer to where it lives instead.
- Never run sudo, su, doas or pkexec. You have no root access; every attempt
  is refused and raises a security alert. Report that the step needs the owner.
- Do not push, deploy, send mail or spend money unless the user asked for it.
- A check that could not run is not a pass. Label each claim: verified (you
  ran it just now), unknown (could not check, and why), or assumed.
- When a version-carrying file changes, bump the version and every place that
  shows it in the same change.

Be concise. Report what is now true and what it unblocks, not how you got there.
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

# Firmer than the old one-line prod: an abliterated thinking model puts its
# whole answer in the reasoning channel and leaves `content` empty. Tell it
# plainly where the answer has to go.
EMPTY_REPLY_NUDGE = (
    "Your last reply was empty -- your answer went into the reasoning channel, "
    "not into the message. Put your answer in the message content, not the "
    "reasoning. Either call a tool to take the next concrete action, or state "
    "plainly in the content what you did, what is left, and the next step. Do "
    "not reply with an empty message.")

# A no-tool wall of prose two turns running: stop narrating and act.
RUNAWAY_NUDGE = (
    "That reply was a long block of text with no action taken. Stop explaining "
    "and do the next concrete thing: call a tool now, or give a two-line status "
    "(what is done, what is next). Do not repeat the explanation.")

# A reply the server stopped at the output cap (num_predict): cut off, not done.
OUTPUT_CAP_NUDGE = (
    "Your last reply hit the output cap and was cut off before it finished. "
    "Do the work in smaller pieces: write a large file in parts of about 200 "
    "lines (write_file, then edit_file to add the rest) and take one step per "
    "tool call. Continue from where it was cut off; do not repeat it.")

# When the empty-reply retries are spent but the model did put something in the
# reasoning channel, surface a short summary of it rather than the whole wall.
_THINKING_SUMMARY_CHARS = 1500


def _truncate_reasoning(thinking: str, limit: int = _THINKING_SUMMARY_CHARS) -> str:
    thinking = thinking.strip()
    if len(thinking) <= limit:
        return thinking
    return thinking[:limit].rstrip() + "\n\n... (reasoning truncated)"


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
        if isinstance(self.client, _RealOllamaClient):
            # Live phase for the status line while a streamed reply arrives.
            self.client.on_progress = (
                lambda phase, info: self.on_event("model_phase", {"phase": phase, **info}))
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
        # Runaway-output cap (2.40.0): a no-tool reply over the char cap two
        # turns running is truncated + nudged. The streak persists across
        # run() calls because keep-going drives the "every turn" recurrence.
        self._big_content_streak = 0
        # Tool-failure circuit breaker (2.40.0): set True when a run stops
        # because consecutive tool failures hit tool_failure_stop_at, so the
        # REPL names that cause rather than letting keep-going nudge on.
        self.tool_circuit_tripped = False
        # One-time tool-choice hints (2.41.0): kinds already given this
        # session, and the current run of consecutive search calls.
        self._hints_given: set[str] = set()
        self._search_run = 0
        # Tool rules sit inside the static prefix: built once here from a
        # PATH probe cached per process, so the prefix never changes mid-session.
        rules = tool_rules_for(set(registry))
        system = SYSTEM_PROMPT + (f"\n\n{rules}" if rules else "")
        system += f"\n\n{system_extra}" if system_extra else ""
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
        self.tool_circuit_tripped = False
        # One compact-and-retry per run for an empty reply that came back with
        # the window full: a context-full reply is not an empty reply.
        context_full_retried = False
        # One nudge per run for a reply cut off at the output cap (2.41.1).
        output_cap_nudged = False
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
            outgoing = self._outgoing()
            sent_estimate = self._estimate_prompt() + (
                estimate_tokens(outgoing[len(self.messages):]))
            started = time.time()
            self.on_event("model_phase", {"phase": "reading", "tokens": sent_estimate})
            reply = self.client.chat(
                outgoing,
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

            if getattr(reply, "truncated", False) and not reply.tool_calls:
                cap = getattr(self.client, "num_predict", 0) or reply.eval_tokens
                self.on_event("output_cap", {"tokens": reply.eval_tokens, "cap": cap,
                                             "nudged": not output_cap_nudged})
                if not output_cap_nudged:
                    output_cap_nudged = True
                    self.messages.append({"role": "user", "content": OUTPUT_CAP_NUDGE})
                    continue
                final = ((reply.content or "").rstrip() +
                         f"\n\n(reply cut off at the output cap of {cap:,} tokens)")
                break

            if not reply.tool_calls:
                content = reply.content or ""
                # (1) EMPTY REPLY -- stall (b), diagnosed 2026-10-02: a
                # thinking-capable local model (qwen3) returns everything in
                # `thinking` with an EMPTY `content` and no tool calls. Nudge up
                # to empty_reply_retries times for a concrete answer or action.
                # The counter resets on any non-empty/tool reply (below), so it
                # is bounded and an always-empty model still terminates here.
                if not content.strip():
                    self._big_content_streak = 0
                    if empty_retries < max(0, self.config.empty_reply_retries):
                        empty_retries += 1
                        self.on_event("empty_reply", {
                            "had_thinking": bool(reply.thinking),
                            "attempt": empty_retries})
                        self.messages.append(
                            {"role": "user", "content": EMPTY_REPLY_NUDGE})
                        continue
                    # Retries spent. If the model DID reason, surface a short
                    # summary (not the whole wall) and let keep-going carry the
                    # task on -- do NOT hard-stop a run that is still working.
                    # Only a truly empty reply (no thinking) stops the run.
                    thinking = (reply.thinking or "").strip()
                    if thinking:
                        final = (_truncate_reasoning(thinking) +
                                 "\n\n(summary of reasoning; no answer was put in "
                                 "the message)")
                        self.on_event("empty_reply_recovered", {"chars": len(thinking)})
                    else:
                        self.last_reply_empty = True
                        final = ""
                    break

                # (3) RUNAWAY OUTPUT: a no-tool wall of prose two turns running.
                # The streak persists across run() calls (keep-going drives the
                # recurrence). Truncate the transcript copy and nudge for action
                # so the "mountain of text" cannot repeat every turn.
                cap = max(0, int(self.config.runaway_content_chars))
                if cap and len(content) > cap:
                    self._big_content_streak += 1
                    if self._big_content_streak >= 2:
                        self.messages[-1]["content"] = (
                            content[:cap].rstrip() +
                            "\n\n... (output truncated by harness: too long, no action taken)")
                        self.on_event("runaway_output", {
                            "chars": len(content), "streak": self._big_content_streak})
                        self.messages.append(
                            {"role": "user", "content": RUNAWAY_NUDGE})
                        empty_retries = 0
                        continue
                    final = content
                    break

                # A normal plain reply ends the turn.
                self._big_content_streak = 0
                final = content
                break

            empty_retries = 0
            self._big_content_streak = 0
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

            # (2) CIRCUIT BREAKER: too many consecutive failing tool calls with
            # no successful write/progress between them. Checkpoint and stop
            # cleanly with a useful summary instead of spinning to the round cap.
            if (self.state.consecutive_tool_failures
                    >= max(1, self.config.tool_failure_stop_at)):
                self.tool_circuit_tripped = True
                final = self._stuck_tool_summary()
                self.on_event("tool_circuit_stop", {
                    "failures": self.state.consecutive_tool_failures})
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

    def _stuck_tool_summary(self) -> str:
        """The clean stop when the circuit breaker trips: say how many calls
        failed in a row, the last error, and a concrete next step, instead of
        spinning silently to the round cap."""
        n = self.state.consecutive_tool_failures
        last = self.state.failures[-1] if self.state.failures else None
        why = f" last error: {last.why}" if last else ""
        what = f" (last call: {last.what})" if last else ""
        return (f"stuck: {n} tool calls failed in a row with no progress"
                f"{what}.{why}\n\nNext step: change the tool, the arguments, or "
                "the approach -- the same call will not start working on another "
                'retry. Type "/continue" to resume with a fresh budget once you '
                "have a different plan.")

    def _outgoing(self) -> list[dict[str, Any]]:
        """The message list sent to the model: the stored transcript, with the
        current harness briefing appended to it as a user-role note whenever
        its text differs from the last briefing in the transcript.

        Ollama reuses its KV cache only for the prompt prefix byte-identical
        to the previous call (2026-10-17: a briefing pinned after the system
        prompt made two turns re-read 48,813 and 49,402 tokens). 2.38.0 moved
        it to the tail but removed and re-sent it each call, so every prompt
        diverged at the old briefing's start. A hybrid/recurrent model
        (qwen35) resumes only from a context checkpoint, and llama.cpp makes
        those only near the end of each prompt (n_tokens-513, n_tokens-1);
        a briefing over ~512 tokens put both past the divergence, so
        2026-10-20 33 of ~50 big-context calls re-read 36-41k tokens (34-40 s).

        Since 2.40.1 briefings are stored and never removed or edited between
        compactions, so each call's list extends the previous call's list
        exactly. It is not a `system` message: Ollama collates system
        messages into the top block, which would bust the prefix.
        """
        self._strip_stored_briefings()
        text = self.state.briefing()
        if text and text != self._last_stored_briefing():
            self.messages.append({"role": "user", "content": text})
        return list(self.messages)

    def _last_stored_briefing(self) -> str | None:
        for m in reversed(self.messages):
            if m.get("role") == "user" and BRIEFING_MARKER in (m.get("content") or ""):
                return m.get("content")
        return None

    def _strip_stored_briefings(self) -> None:
        """Drop briefing copies a pre-2.38 session stored in the transcript
        (pinned after the system prompt); the live one is added per call."""
        if any(BRIEFING_MARKER in (m.get("content") or "") and m.get("role") == "system"
               for m in self.messages[1:]):
            self.messages = [self.messages[0]] + [
                m for m in self.messages[1:]
                if not (m.get("role") == "system"
                        and BRIEFING_MARKER in (m.get("content") or ""))]

    def compact_now(self) -> tuple[int, int, bool]:
        """Compact on demand (the REPL's /compact). Returns (tokens before,
        tokens after, whether anything changed)."""
        before_msgs = len(self.messages)
        before = self._prompt_size()
        self._maybe_compact(force=True, reason="manual")
        changed = len(self.messages) != before_msgs
        return before, self._estimate_prompt(), changed

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
        signature = f"{call.name}({self._arg_signature(call)})"
        if tool is None:
            available = ", ".join(sorted(self.known))
            self.on_event("tool_error", {"name": call.name})
            self.state.consecutive_tool_failures += 1
            return f"No tool named {call.name!r}. Available tools: {available}"

        # CIRCUIT BREAKER (2.40.0): once this exact call has failed
        # tool_failure_block_at times, hard-block it -- do NOT run it again.
        # A local model otherwise reissues the identical failing call forever.
        block_at = max(1, int(getattr(self.config, "tool_failure_block_at", 3)))
        if self.state.repeated(signature) >= block_at:
            self.state.consecutive_tool_failures += 1
            self.on_event("tool_blocked", {"name": call.name, "signature": signature})
            return ("[harness] blocked: this exact call failed "
                    f"{block_at} times; it will not run again -- change the tool, "
                    "arguments, or approach.")

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
            self.state.record_failure(signature, result_text)
            self.state.consecutive_tool_failures += 1
            return result_text
        except Exception as exc:  # a tool crash must not kill the session
            result_text = f"{call.name} raised {type(exc).__name__}: {exc}"
            self.on_event("tool_end", {"name": call.name, "error": True,
                                       "output": result_text,
                                       "seconds": time.time() - started})
            self.state.record_failure(signature, result_text)
            self.state.consecutive_tool_failures += 1
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
            self.state.record_failure(signature, result.output)
            self.state.consecutive_tool_failures += 1
            n = self.state.repeated(signature)
            if n > 1:
                return (result.output +
                        f"\n\n[harness] This is attempt {n} at the same call and it has "
                        f"failed every time. Change the approach rather than retrying.")
            return result.output

        # A successful tool call is real progress: reset the consecutive-failure
        # circuit-breaker counter and the stuck signals (wilbur.horizon).
        self.state.consecutive_tool_failures = 0
        self.state.last_tool = signature
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
        return result.output + self._tool_hint(call)

    def _tool_hint(self, call: ToolCall) -> str:
        """A one-time steer appended to a successful result when the call took
        the expensive road (wilbur.directives). Rides in the tool result, never
        the system prompt, so the cached prefix is untouched."""
        if not getattr(self.config, "tool_hints", True):
            return ""
        args = call.arguments or {}
        searching = directives.is_search_call(call.name, args)
        self._search_run = (self._search_run + 1) if searching else 0
        kind = directives.classify(call.name, args, self._search_run, "task" in self.registry)
        if kind is None or kind in self._hints_given:
            return ""
        self._hints_given.add(kind)
        self.on_event("tool_hint", {"name": call.name, "kind": kind})
        return "\n\n" + directives.HINTS[kind]

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
