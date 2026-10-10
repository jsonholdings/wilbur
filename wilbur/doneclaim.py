"""Never accept a "done" claim without evidence.

Live run, 2026-10-02 (qwen3:14b, a 4-item calc task): the model wrote three
files, replied "the tests have passed" without running pytest, never ticked its
plan (0/4), then answered six generic "continue" nudges with "All items are
complete. No further action is needed." -- no tool call, until the stuck
detector stopped it. A generic nudge cannot fix that; evidence can.

So when a turn ends with a completion claim (or with no tool call) while plan
items are still open, the harness checks for itself:

- runs the project's own check (wilbur.verify.detect -- never an invented
  command), and
- checks that every file named in an open plan item exists.

Passed + nothing missing + the model claimed completion: tick the plan and stop
as COMPLETE, with the evidence shown. Failed or missing: the next nudge is
specific -- the failing command, its output tail, the exact open items, and
"don't claim completion without running the check".

The claims guard: a reply that says the tests pass when no test command ran in
that turn is flagged "unverified claim" and triggers the same check.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import verify
from .state import RunState

# A claim that the work is finished (not "item 1 done", not "b.txt created").
_DONE_CLAIM = re.compile(
    r"\b(all (three |four |3 |4 |the |of the )?(items|tasks|steps|files)"
    r"( are| have been)?( now)? (done|complete|completed|finished|created)|"
    r"(everything|all) (is |are )?(now )?(done|complete|finished)|"
    r"(completed|finished|done with) all\b|"
    r"no further (action|work|changes?) (is |are )?(needed|required)|"
    r"(the |this )?(task|work|implementation|project) is (now )?(complete|done|finished)|"
    r"(is|are) (now )?(fully )?(functional|ready to use|working))",
    re.IGNORECASE)

# A claim that tests ran and passed.
_TESTS_PASS = re.compile(
    r"\b(tests?|test suite|pytest|checks?)\b[^.\n]{0,40}?\b"
    r"(pass(es|ed)?|succeed(s|ed)?|green|all ok)\b"
    r"|\ball (\d+ )?tests? (pass|passed|succeeded)\b",
    re.IGNORECASE)

# A shell command that runs a test suite.
_TEST_CMD = re.compile(
    r"(\bpytest\b|-m\s+pytest|-m\s+unittest|\bmake\s+(test|check)\b|"
    r"\bnpm\s+(run\s+)?(-s\s+|--silent\s+)?test\b|\byarn\s+test\b|"
    r"\bcargo\s+test\b|\bgo\s+test\b|\btox\b|\bnox\b)")

# A file path named in a plan item: has a directory or a short extension.
_PATH = re.compile(r"(?<![\w/.-])([\w.-]+(?:/[\w.-]+)*\.[A-Za-z]{1,5})(?![\w/-])")

TAIL_CHARS = 1200


def claims_done(reply: str) -> bool:
    return bool(_DONE_CLAIM.search(reply or ""))


def claims_tests_pass(reply: str) -> bool:
    return bool(_TESTS_PASS.search(reply or ""))


def is_test_command(command: str) -> bool:
    return bool(_TEST_CMD.search(command or ""))


def open_items(state: RunState | None) -> list[str]:
    if state is None:
        return []
    return [i.text for i in state.plan if not i.done]


def named_files(texts: list[str]) -> list[str]:
    seen: list[str] = []
    for text in texts:
        for m in _PATH.finditer(text):
            path = m.group(1).rstrip(".")
            # "e.g." / version numbers are not files.
            if re.fullmatch(r"[\d.]+", path) or path.lower() in ("e.g", "i.e"):
                continue
            if path not in seen:
                seen.append(path)
    return seen


@dataclass
class Verdict:
    passed: bool | None            # the project check; None = no check / could not run
    label: str = ""                # e.g. "pytest"
    output: str = ""
    missing: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.passed is True and not self.missing

    def evidence(self) -> str:
        last = (self.output.strip().splitlines() or [""])[-1][:160]
        return f"{self.label} passed ({last})" if self.passed else ""

    def nudge(self, unverified_claim: bool = False) -> str:
        """The specific nudge: what failed, its output tail, the open items."""
        out = ["[harness] Your work is not verified as done."]
        if unverified_claim:
            out.append("Unverified claim: you said the tests pass, but no test "
                       "command ran in that turn.")
        if self.passed is False:
            tail = self.output[-TAIL_CHARS:]
            out += [f"The project check FAILED: `{self.label}`", "Output (tail):",
                    tail.rstrip()]
        elif self.passed is None:
            out.append("No project check could be run" +
                       (f" ({self.output[:200]})" if self.output else "") +
                       ". Run the tests yourself with run_bash.")
        else:
            out.append(f"The project check passed: `{self.label}`.")
        if self.missing:
            out.append("Files named in your plan that do not exist: " +
                       ", ".join(self.missing))
        if self.items:
            out.append("Open plan items:")
            out += [f"  [ ] {t}" for t in self.items]
        out.append("Use tools to fix this now. Do not claim completion without "
                   "running the check; when an item is really done, mark it "
                   "completed with todo_write.")
        return "\n".join(out)


def check(cwd: str, state: RunState | None, timeout: int = verify.DEFAULT_TIMEOUT) -> Verdict:
    """Run the project's own check and look for the files the open items name."""
    items = open_items(state)
    missing = [p for p in named_files(items) if not (Path(cwd) / p).exists()]
    found = verify.detect(cwd)
    if found is None:
        return Verdict(None, "", "no test command detected", missing, items)
    passed, output = verify.run(found, cwd, timeout=timeout)
    return Verdict(passed, found.label, output, missing, items)


def tick_all(state: RunState) -> int:
    """Mark every open plan item completed; return how many were ticked."""
    n = 0
    for item in state.plan:
        if not item.done:
            item.status = "completed"
            n += 1
    return n
