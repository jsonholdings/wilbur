"""Bash execution with a deny-list and hard output clamping."""
from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import time
from typing import Any

from ..inbox import DEFAULT_BOUND_S, is_wait_command, poll_interval
from .base import Approval, ToolContext, ToolResult, clamp, denial_message


# Wilbur runs unattended as an ordinary user. Every attempt to elevate fires a
# root-mail security alert on the host (seen 2026-10-03 and 2026-10-10: the
# loop model ran `sudo /usr/bin/true` in every task). Refuse before executing.
ELEVATION_PROGRAMS = frozenset({"sudo", "su", "doas", "pkexec", "run0", "sudoedit"})
# Wrappers whose first non-option argument is itself the program to run.
_WRAPPERS = frozenset({"env", "command", "exec", "nohup", "time", "nice",
                       "ionice", "timeout", "stdbuf", "setsid", "xargs",
                       "builtin", "chronic", "unbuffer", "flatpak-spawn"})
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|\n(){}`]|\$\(")
ELEVATION_REFUSAL = (
    "Refused without running: this command invokes {prog!r}. Root access is not "
    "available to Wilbur and every attempt raises a security alert on the host. "
    "Do not retry it or any other sudo/su/doas/pkexec form. Do the work as the "
    "current user, or report that the step needs the owner."
)


def _program_tokens(segment: str) -> list[str]:
    try:
        tokens = shlex.split(segment, comments=True)
    except ValueError:
        tokens = segment.split()
    programs: list[str] = []
    index = 0
    while index < len(tokens):
        word = tokens[index]
        if "=" in word and word.split("=", 1)[0].isidentifier():
            index += 1
            continue
        programs.append(word)
        if os.path.basename(word) not in _WRAPPERS:
            break
        index += 1
        # skip the wrapper's options and numeric/duration arguments
        while index < len(tokens) and (tokens[index].startswith("-")
                                       or re.match(r"^[0-9.]+[smhd]?$", tokens[index])):
            index += 1
    return programs


def elevation_program(command: str) -> str | None:
    """Return the elevation program a command would run, or None."""
    for segment in _SEGMENT_SPLIT.split(command):
        for word in _program_tokens(segment.strip()):
            name = os.path.basename(word.strip("'\"\\"))
            if name in ELEVATION_PROGRAMS:
                return name
    return None


class Bash:
    name = "run_bash"
    read_only = False
    description = (
        "Run a shell command and return its combined stdout and stderr. "
        "Use for builds, tests, git, and any command-line tool. Prefer the "
        "dedicated search tools over find and grep."
    )
    schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run"},
            "timeout": {"type": "integer", "description": "Seconds, default 120"},
            "description": {"type": "string",
                            "description": "5-10 word description of what this does"},
        },
        "required": ["command"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args.get("command", "").strip()
        if not command:
            return ToolResult("No command given.", is_error=True)

        elevated = elevation_program(command)
        if elevated:
            return ToolResult(ELEVATION_REFUSAL.format(prog=elevated), is_error=True)

        lowered = command.lower()
        for pattern in ctx.config.denied_bash:
            if pattern.lower() in lowered:
                return ToolResult(
                    f"Refused: command matches the deny-list entry {pattern!r}.",
                    is_error=True,
                )

        label = args.get("description") or command[:70]
        outcome = ctx.approve("run_bash", f"{label}\n    $ {command}")
        if outcome is not Approval.GRANTED:
            return ToolResult(denial_message("command", outcome), is_error=True)

        timeout = int(args.get("timeout", 120))
        inbox = getattr(ctx, "inbox", None)
        if inbox is not None and is_wait_command(command):
            return self._run_wait(command, ctx, timeout, inbox)
        try:
            proc = subprocess.run(
                command, shell=True, cwd=ctx.cwd, capture_output=True,
                text=True, timeout=timeout, errors="replace",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(f"Command timed out after {timeout}s.", is_error=True)

        body = (proc.stdout or "") + (proc.stderr or "")
        body = clamp(body.strip(), ctx.config.max_tool_output_chars,
                     ctx.config.max_tool_output_lines)
        if proc.returncode != 0:
            return ToolResult(f"exit {proc.returncode}\n{body}" if body
                              else f"exit {proc.returncode} (no output)", is_error=True)
        return ToolResult(body or "(no output)")

    @staticmethod
    def _run_wait(command: str, ctx: ToolContext, timeout: int, inbox) -> ToolResult:
        """A sleep/wait command, polled so a pending user message (or
        Ctrl-C) pre-empts it instead of holding the message for the whole
        wait (bounded message check, wilbur.inbox)."""
        step = poll_interval(getattr(ctx.config, "message_check_max_s", DEFAULT_BOUND_S))
        proc = subprocess.Popen(command, shell=True, cwd=ctx.cwd, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                errors="replace", start_new_session=True)
        deadline = time.monotonic() + timeout
        why = ""
        while True:
            try:
                out, _ = proc.communicate(timeout=step)
                break
            except BaseException as exc:
                if not isinstance(exc, subprocess.TimeoutExpired):
                    # Ctrl-C: the wait runs in its own session, so SIGINT
                    # never reached it -- stop it here, not leave an orphan.
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except OSError:
                        pass
                    raise
                cancel = getattr(ctx, "cancel", None)
                if inbox.pending():
                    why = "Wait interrupted: a new message from the user arrived."
                elif cancel is not None and cancel.is_set():
                    why = "Wait interrupted: cancelled."
                elif time.monotonic() >= deadline:
                    why = f"Command timed out after {timeout}s."
                if why:
                    _stop_group(proc)
                    return ToolResult(why, is_error=not why.startswith("Wait interrupted"))
        body = (out or "").strip() or "(no output)"
        if proc.returncode != 0:
            return ToolResult(f"exit {proc.returncode}\n{body}", is_error=True)
        return ToolResult(body)


def _stop_group(proc: subprocess.Popen) -> None:
    """SIGTERM the wait's process group, SIGKILL it if it ignores that."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue
