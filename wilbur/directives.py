"""Tool-use directives: which of Wilbur's tools to reach for, and when.

Measured on 44 saved sessions (2026-10-21): run_bash 954 calls against grep 15
and glob 4, with 424 of those shell calls being grep/find/ls; pytest typed into
run_bash 45 times against run_tests 9; list_skills/load_skill never called;
gate-run never used; 17 of 47 "done" replies came after a write with no test
run since. The tools existed -- nothing told the model when they pay off.

Two halves, both cheap:

- TOOL_RULES: compact conditional rules in the system prompt (rule -> tool,
  with the "when not" spelled out so a small task is not over-tooled).
- host rules: lines for host helpers (gate-run, tb, session-claim) that are
  added ONLY when the command is on PATH, so the public build never tells a
  model to call a program it does not have. Probed once per process and
  cached, so the static prompt prefix never changes mid-session (2.40.1).
- tool_hint(): a one-time, appended note on a tool RESULT when a call took
  the expensive road (shell grep instead of the grep tool, bare pytest instead
  of run_tests, a long run of searches that belonged in a task). It rides in
  the tool result, never the system prompt, so the cached prefix is untouched.
"""
from __future__ import annotations

import functools
import re
import shutil

TOOL_RULES_HEAD = "Tool choice (take the cheap road; a one-step job needs no extra tool):"

# (tool the rule needs, rule). A rule whose tool is not in an agent's registry
# is left out, so a restricted subagent is never told to call a missing tool.
TOOL_RULES = (
    ("grep", "- Find code -> grep / glob, not run_bash grep/find/ls: they return file:line."),
    ("read_file", "- Read a file once; re-read only after editing it, and then only the lines you need (offset/limit)."),
    ("run_tests", "- Run tests -> run_tests (compact summary, file:line per failure), not raw pytest in run_bash. "
                  "One test file while iterating, the full suite before you call it done."),
    ("run_tests", "- About to say done, fixed or passing -> run_tests (or the project's check) first, this turn; "
                  "the harness re-checks any claim made without it."),
    ("task", "- Search or change spanning many files you need not watch -> task, with a standalone brief. "
             "One or two files -> do it inline."),
    ("list_skills", "- Unfamiliar kind of job (tests, changelog, benchmark) -> list_skills once; load_skill only if one matches."),
    ("write_file", "- Large file or long script -> write it in parts of ~200 lines or fewer "
                   "(write_file, then edit_file); split a big step into several tool calls."),
    ("run_bash", "- Risky multi-file change in a tree with uncommitted work you did not make -> "
                 "`git worktree add` on a new branch, not in place."),
)

# (command that must be on PATH, tool the rule needs, rule).
_HOST_RULES = (
    ("gate-run", "run_tests", "- Full suite on this host -> run_tests with command "
                              "`gate-run --max-load 8 <test command>` (queues behind load; never raise the limit)."),
    ("tb", "run_bash", "- Missing tool (php, reportlab, headless chrome, pdftoppm, zbarimg) -> `tb <cmd>`; never install it."),
    ("session-claim", "run_bash", "- Before the first write in a project -> `session-claim check .` (exit 3 = held: stop and report)."),
)


@functools.lru_cache(maxsize=1)
def host_commands() -> frozenset[str]:
    """Host helpers present on PATH; probed once per process so the prompt
    prefix built from it never changes mid-session."""
    return frozenset(cmd for cmd, _, _ in _HOST_RULES if shutil.which(cmd))


def tool_rules_for(tools: set[str], host: frozenset[str] | None = None) -> str:
    """The rule block for an agent holding `tools`; empty when none apply."""
    host = host_commands() if host is None else host
    lines = [rule for need, rule in TOOL_RULES if need in tools]
    lines += [rule for cmd, need, rule in _HOST_RULES if cmd in host and need in tools]
    return "\n".join([TOOL_RULES_HEAD, *lines]) if lines else ""


_SHELL_SEARCH = re.compile(r"^\s*(?:cd\s+\S+\s*&&\s*)?(grep|egrep|rg|find|ls\s+-[a-zA-Z]*R)\b")
_BARE_TEST = re.compile(r"(\bpytest\b|-m\s+pytest|\bnpm\s+(run\s+)?test\b|\bmake\s+test\b)")
# Host-hardware surgery that resets/removes a device on a SHARED controller:
# unbind/bind/remove a PCI or USB controller or driver via sysfs, or remove a
# host kernel module. A --privileged container doing this needs no sudo, so the
# no-root prompt rule never catches it. Matches the /sys path even inside a
# `docker run --privileged sh -c '...'`. 2026-10-24: an xhci unbind dropped the
# Alfa adapters (and sat on the same controller as the keyboard and YubiKey).
_HOST_HW = re.compile(r"/sys/bus/(?:pci|usb)/\S*(?:unbind|bind|remove)"
                      r"|/sys/devices/\S*(?:unbind|remove)"
                      r"|\bmodprobe\s+-r\b")
SEARCH_TOOLS = {"grep", "glob", "list_files"}
SEARCH_RUN_FOR_TASK = 6

HINTS = {
    "shell_search": "[harness] tip: the grep and glob tools search faster and return file:line; use them instead of shell grep/find.",
    "bare_tests": "[harness] tip: run_tests runs this and returns a compact pass/fail summary with file:line per failure.",
    "delegate": "[harness] tip: that is many searches in a row; a broad search belongs in the task tool, which returns only the conclusion.",
    "host_hw": "[harness] danger: unbinding/binding/removing a host PCI/USB controller or driver (or modprobe -r) resets that controller -- a shared one may also carry the keyboard, disks or security keys, which drop with it. To give a container a device, pass it in (--device, --net=host, cgroup device rules); do not reset the host. If this failed, the approach is wrong: stop and report, do not retry it.",
}


def classify(name: str, args: dict, search_run: int, has_task: bool) -> str | None:
    """Which hint (if any) this successful call earns. Pure; no state."""
    if name == "run_bash":
        command = str((args or {}).get("command", ""))
        if _HOST_HW.search(command):
            return "host_hw"
        if _SHELL_SEARCH.search(command):
            return "delegate" if has_task and search_run >= SEARCH_RUN_FOR_TASK else "shell_search"
        if _BARE_TEST.search(command) and "gate-run" not in command:
            return "bare_tests"
        return None
    if name in SEARCH_TOOLS and has_task and search_run >= SEARCH_RUN_FOR_TASK:
        return "delegate"
    return None


def is_search_call(name: str, args: dict) -> bool:
    if name in SEARCH_TOOLS:
        return True
    return name == "run_bash" and bool(_SHELL_SEARCH.search(str((args or {}).get("command", ""))))
