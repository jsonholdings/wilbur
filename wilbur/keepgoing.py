"""Keep going: auto-continue a plain prompt with several work items.

Why this exists: `Agent.run` ends the turn on the first model reply with no
tool call (agent.py, `if not reply.tool_calls: break`). Local models routinely
finish item 1 of 3, say "Next, I'll create add.py", and stop -- so the owner had
to type "continue" between items (2026-10-02). `/loop` fixes that for a goal
with a done-check; this fixes it for the ordinary prompt.

`decide` looks at the finished turn and says whether to nudge the model on:

- continue when the model went quiet mid-task: an empty reply after a turn
  that called tools (the commonest real stop, measured on the owner's saved
  sessions 2026-10-02; `worked` says the turn did tool work),
- continue when the reply only asks permission to carry on with the request
  ("Shall I continue with the next item?") -- the nudge itself says "if you
  need a decision from me, ask it", so a real question still gets asked,
- stop when the reply asks the user something else (a choice, an offer of new
  scope, or "let me know"),
- stop when the reply says everything is done,
- continue when the model's own todo plan still has open items,
- continue when the reply announces a next step ("Next, I'll ...").

The REPL owns the caps (turns + wall clock), Ctrl-C, approvals and the
"↻ continuing (n/N)" line; see `Repl._keep_going`. The nudge never approves
anything: every tool call still goes through the normal approval prompt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .state import RunState

NUDGE = ("Continue with the next item. If every item is already done, say so "
         "plainly and stop; if you need a decision from me, ask it.")

_QUESTION = re.compile(
    r"(\?\s*[*_`)\"']*\s*$)"
    r"|\b(would you like|do you want|shall i|should i|let me know|"
    r"please confirm|which (one|option) (do|would) you|your call)\b",
    re.IGNORECASE)

_ALL_DONE = re.compile(
    r"\b(all (three |3 |the |of the )?(items|tasks|steps)( are| have been)? "
    r"(done|complete|completed|finished)|"
    r"(everything|all) (is |are )?(now )?(done|complete|finished)|"
    r"(completed|finished|done with) all\b)",
    re.IGNORECASE)

# The model says it is holding (usually because the user said so): never nudge past it.
_HOLDING = re.compile(
    r"\b(stopp(ed|ing) (here|there|now)|ok,? stopped|paused|holding off|"
    r"as you asked|waiting (for|on) you)\b", re.IGNORECASE)

_NEXT_STEP = re.compile(
    r"\b(next,? (i'?ll|i will|i'm going to|let me|we'll|is|up)|"
    r"(i'?ll|i will|let me) (now |next )?(move on|proceed|continue|start|create|"
    r"write|add|do|implement|handle|work on|tackle)|"
    r"now (i'?ll|i will|let me)|moving on to|proceeding (to|with)|"
    r"next (step|item|task) (is|will be)|on to (item|step|task)\b)",
    re.IGNORECASE)


# Asking leave to carry on with the request itself, not to choose or to add
# scope: "Shall I proceed?", "Want me to continue with the next item?". A line
# that offers a choice ("A or B?", "which ...") never counts.
_ASKS_TO_PROCEED = re.compile(
    r"\b(shall i|should i|do you want me to|would you like me to|want me to|"
    r"may i|can i|ok(ay)? to|ready to|let me know if (you want me to|i should))\s+"
    r"(now\s+)?(continue|proceed|go ahead|move on|carry on|keep going|go on|resume)"
    r"(\s+(with|to|on to|onto)\s+(the\s+)?(next|remaining|rest|other|item|step|"
    r"task|part|phase|\d)[^?.!]*)?\s*[?.!]?\s*[*_`)\"']*\s*$",
    re.IGNORECASE)
_CHOICE = re.compile(r"\b(or|which|prefer|either)\b", re.IGNORECASE)


@dataclass
class Decision:
    go: bool
    reason: str


def _tail(reply: str, chars: int = 600) -> str:
    return (reply or "").strip()[-chars:]


def asks_user(reply: str) -> bool:
    """A question or request for the user's input, judged on the reply's end
    (a question earlier on, already answered by the model's own work, does not
    count)."""
    tail = _tail(reply, 300)
    last = tail.splitlines()[-1] if tail else ""
    return bool(_QUESTION.search(last)) or bool(
        re.search(r"\b(let me know|would you like|do you want|shall i|please confirm)\b",
                  tail, re.IGNORECASE))


def asks_to_proceed(reply: str) -> bool:
    """The reply's last line only asks leave to carry on with the request."""
    tail = _tail(reply, 300)
    last = tail.splitlines()[-1].strip() if tail else ""
    return bool(_ASKS_TO_PROCEED.search(last)) and not _CHOICE.search(last)


def decide(reply: str, state: RunState | None, worked: bool = False) -> Decision:
    """`worked`: the turn that produced `reply` called at least one tool."""
    text = (reply or "").strip()
    if text == "Cancelled by user.":
        return Decision(False, "cancelled")
    if not text:
        if worked:
            return Decision(True, "went quiet mid-task")
        return Decision(False, "empty reply")
    open_items = 0
    if state is not None and state.plan:
        done, total = state.progress
        open_items = total - done
    all_done = bool(_ALL_DONE.search(_tail(reply))) and open_items == 0
    if asks_user(reply):
        if asks_to_proceed(reply) and not all_done:
            return Decision(True, "asked leave to carry on")
        return Decision(False, "asked you a question")
    if _HOLDING.search(_tail(reply, 300)):
        return Decision(False, "holding")
    if all_done:
        return Decision(False, "all items done")
    if open_items:
        return Decision(True, f"plan {total - open_items}/{total} done")
    if _NEXT_STEP.search(_tail(reply)):
        return Decision(True, "announced a next step")
    return Decision(False, "finished")
