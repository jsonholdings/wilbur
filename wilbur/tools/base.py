"""Tool contract and the output clamp every tool result passes through."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol


class Approval(str, Enum):
    """What happened when a tool asked the user for permission.

    A plain bool collapsed "the user said no" and "we never actually asked"
    into the same signal, which let the model claim a denied call had run
    (2026-09-18: a batch of ^C-cancelled prompts came back as `False` and the
    model reported "all 8 came back together"). Every non-GRANTED value must
    produce a distinct, unambiguous message -- see `denial_message` below.
    """

    GRANTED = "granted"
    DENIED = "denied"            # the user was asked and answered no
    INTERRUPTED = "interrupted"  # Ctrl-C / KeyboardInterrupt while the prompt was up
    UNAVAILABLE = "unavailable"  # no interactive stdin to ask on (EOF, non-tty)


def denial_message(what: str, outcome: "Approval") -> str:
    """A message the model cannot mistake for the call having run."""
    if outcome is Approval.DENIED:
        return f"User denied the {what}. It did not run. Do not repeat it without asking."
    if outcome is Approval.INTERRUPTED:
        return (f"Approval for the {what} was interrupted (Ctrl-C) before the user answered. "
                f"It did NOT run and was not denied on the merits -- ask again or propose a "
                f"different approach.")
    if outcome is Approval.UNAVAILABLE:
        return (f"The {what} could not be approved: no interactive input was available to ask "
                f"the user. It did not run.")
    return f"The {what} was not approved and did not run."


# Prefix of the approval detail for a write/edit whose target is outside the
# session's working directory. `--approve auto-edit` and `auto_approve` grant
# edits inside the cwd only, like Claude Code's acceptEdits mode; an edit
# outside it is asked about. 2026-11-23: an auto-approved bench run wrote
# integrity.py into the Wilbur repo root, outside its task directory.
OUTSIDE_CWD = "OUTSIDE the working directory: "

# Tools that write files and so fall under auto-edit / auto_approve rules.
# apply_patch refuses outside-cwd paths itself, so it never carries OUTSIDE_CWD.
EDIT_TOOLS = frozenset({"edit_file", "write_file", "apply_patch"})


def outside_cwd(path: Path, cwd: str) -> bool:
    """True when `path` is not under `cwd` (both resolved, symlinks followed)."""
    try:
        path.resolve().relative_to(Path(cwd).expanduser().resolve())
    except ValueError:
        return True
    except OSError:
        return True
    return False


@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    # Set when a tool wants to say something to the user that should not be
    # spent as model context (e.g. "wrote 340 lines to foo.py").
    display: str = ""
    # Display-only before/after text for write_file/edit_file, rendered as a
    # coloured diff in the terminal: {"path", "old", "new", "start", "created"}.
    # Never sent to the model or saved in the session.
    diff: dict | None = None


class Tool(Protocol):
    name: str
    description: str
    schema: dict[str, Any]
    read_only: bool

    def run(self, args: dict[str, Any], ctx: "ToolContext") -> ToolResult: ...


@dataclass
class ToolContext:
    cwd: str
    config: Any
    # Files the agent has read this session, path -> mtime at read time.
    # Mirrors Claude Code's rule that a file must be read before it is edited,
    # which stops a model from overwriting a file it has never seen.
    read_files: dict[str, float]
    approve: Callable[[str, str], Approval]
    # Harness-owned run state (objective, plan, failure ledger). Optional so a
    # tool can be exercised in isolation without standing up a whole agent.
    state: Any = None
    # threading.Event set to request cooperative cancellation (Ctrl-C reaching
    # a subagent running on its own thread). Optional for the same reason.
    cancel: Any = None
    # wilbur.inbox.Inbox: lets a long tool call notice a pending user
    # message (a sleep/wait is pre-empted). Optional for the same reason.
    inbox: Any = None


def clamp(text: str, max_chars: int, max_lines: int) -> str:
    """Bound a tool result so one command cannot consume the whole window.

    Keeps the head and tail, which is where the useful signal in command
    output almost always is, and states plainly what was removed.
    """
    lines = text.splitlines()
    if len(lines) > max_lines:
        head = lines[: max_lines * 2 // 3]
        tail = lines[-(max_lines // 3) :]
        omitted = len(lines) - len(head) - len(tail)
        lines = head + [f"... [{omitted} lines omitted] ..."] + tail
        text = "\n".join(lines)
    if len(text) > max_chars:
        keep = max_chars // 2
        text = (
            text[:keep]
            + f"\n... [{len(text) - max_chars} characters omitted] ...\n"
            + text[-keep:]
        )
    return text
