"""Team mode: plan -> code -> test -> review -> merge, one role at a time.

A single agent editing multiple files unsupervised on a small local model is
exactly the case that produces a change which satisfies the letter of the
instruction while breaking something adjacent (see BACKLOG
`critic-pass-before-accepting-a-diff`). Team mode splits that one agent into
four roles with fresh, narrow contexts -- planner, tester, coder, reviewer --
each of which only sees what it needs:

- The coder works in its own git worktree/branch (`wilbur/worktree.py`), never
  the user's checked-out working tree.
- The tester writes tests from the PLAN, not the code, so a test that only
  restates what the coder happened to write cannot pass review.
- The reviewer sees the plan, the diff, and the deterministic gate results --
  never the coder's reasoning transcript -- so it is judging the *change*,
  not agreeing with the coder's own story about the change.
- The main agent re-runs the gates itself before merging; a reviewer verdict
  is advisory, the gates are the actual gate.

There is one GPU. Roles run sequentially against the resident model, each
with its own fresh `Agent` instance (fresh context, no shared transcript).
`reviewer_model` lets the reviewer use a different model, but only if it fits
in free VRAM at the time -- checked, not assumed -- with a clean fallback to
the main model otherwise.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import verify, worktree
from .agent import Agent
from .config import Config
from .tools.base import Approval

CODE_TOOLS = ["read_file", "list_files", "glob", "grep", "write_file",
              "edit_file", "run_bash"]
READ_ONLY_TOOLS = ["read_file", "list_files", "glob", "grep"]

# A file token needs a stem of 2+ chars before a letter extension, so
# abbreviations ("e.g.", "i.e.") and version numbers ("2.22.3") don't count.
_FILE_LIKE = re.compile(r"(?<![\w.])(?:[\w-]+/)*[\w-]{2,}\.[a-zA-Z]{1,5}\b")
_RISKY_LANGUAGE = re.compile(
    # "refactor this function" scoped to one function/method stays single.
    r"\b(refactor(?:ing)?(?!\s+(?:this|that|the|a|one)\s+"
    r"(?:function|method|helper)\b)|across the codebase|multiple files|"
    r"several files|rewrite|migrate|migration|redesign|restructure|"
    r"every (file|module)|throughout the|"
    # multi-component language naming no files
    r"end[- ]to[- ]end|wire\b.+\binto|and the tests|across the \w+, \w+|"
    r"across (?:all|every))\b",
    re.IGNORECASE,
)


def needs_team(prompt: str, team_mode: str) -> bool:
    """Size gate: does this task warrant a coder/tester/reviewer team?

    "on"/"off" are explicit overrides so `/team on|off` always does what it
    says. "auto" is a heuristic: more than one distinct file-looking token,
    or language describing a broad/risky change, routes to the team; a
    single small ask runs single-agent, because a team burns three extra
    model calls on a GPU that may be shared with other local processes.
    """
    mode = (team_mode or "auto").lower()
    if mode == "on":
        return True
    if mode == "off":
        return False
    files = set(_FILE_LIKE.findall(prompt))
    if len(files) > 1:
        return True
    return bool(_RISKY_LANGUAGE.search(prompt))


@dataclass
class GateResult:
    label: str
    passed: bool
    output: str = ""


@dataclass
class TeamResult:
    # "merged" | "approved" | "denied_ask_user" | "gate_failed" | "timeout" |
    # "error" | "cancelled" | "tester_no_tests" | "tester_no_red"
    status: str
    plan: str = ""
    diff: str = ""
    gate_results: list[GateResult] = field(default_factory=list)
    review_notes: list[str] = field(default_factory=list)
    revisions: int = 0
    # Wall-clock seconds per role run, in order ("planner", "tester", ...);
    # a revise loop appends "coder-revise" again.
    role_seconds: list[tuple[str, float]] = field(default_factory=list)
    # "red->green": the tester's tests failed before the coder ran (a real
    # red). "no-red": they already passed, so they do not exercise the change.
    # "no-check": no project check exists, so nothing was gated at all.
    red_green: str = ""
    # Non-test files the tester wrote; reverted before the red gate.
    tester_reverted: list[str] = field(default_factory=list)
    # Every tool call the tester made: {"role", "name", "args", "status"}.
    tester_calls: list[dict] = field(default_factory=list)
    merge_sha: str = ""
    summary: str = ""
    # Every reviewer verdict in order, e.g. ["DENY", "APPROVE"] after one revise.
    verdicts: list[str] = field(default_factory=list)
    # Why the reviewer did not get `reviewer_model` (VRAM fallback), or which
    # model it used; empty when none was configured.
    reviewer_model_note: str = ""


_TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs", "testdata"}
_TEST_FILE = re.compile(r"(^test_.*|.*_test\.\w+$|.*\.(test|spec)\.\w+$|^conftest\.py$)")


def is_test_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return any(p in _TEST_DIRS for p in parts[:-1]) or bool(_TEST_FILE.match(parts[-1]))


def confine_to_tests(wt_path: str) -> list[str]:
    """Undo every non-test change the tester left in its worktree; return those paths.

    Live run 2026-10-02 (qwen3.8 27b): handed the task prompt, the tester
    implemented the whole feature alongside its tests, so the red gate already
    passed, the coder had nothing to do and red-before-green was never real.
    The tester's own prompt is not enough to stop that; this is the gate.
    """
    out = subprocess.run(
        ["git", "-C", wt_path, "status", "--porcelain", "-z", "--untracked-files=all"],
        capture_output=True, text=True,
    ).stdout
    reverted: list[str] = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            i += 1  # -z puts a rename's source path in the next field
        if is_test_path(path):
            continue
        if code == "??":
            try:
                os.remove(os.path.join(wt_path, path))
            except OSError:
                continue
        else:
            subprocess.run(["git", "-C", wt_path, "checkout", "HEAD", "--", path],
                           capture_output=True, text=True)
        reverted.append(path)
    return reverted


def changed_test_paths(wt_path: str, base: str) -> list[str]:
    """Test files that differ from `base`: committed since, staged, modified or untracked."""
    paths: set[str] = set()
    committed = subprocess.run(["git", "-C", wt_path, "diff", "--name-only", base],
                               capture_output=True, text=True).stdout
    paths.update(line for line in committed.splitlines() if line)
    untracked = subprocess.run(
        ["git", "-C", wt_path, "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True).stdout
    paths.update(line for line in untracked.splitlines() if line)
    return sorted(p for p in paths if is_test_path(p))


def _args_summary(args: Any, limit: int = 120) -> str:
    """Short one-line view of a tool call's arguments (file contents elided)."""
    if not isinstance(args, dict):
        text = str(args)
    else:
        parts = []
        for key, value in args.items():
            value = str(value)
            if key in ("content", "new_string", "old_string") and len(value) > 40:
                value = f"<{len(value)} chars>"
            parts.append(f"{key}={value}")
        text = ", ".join(parts)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


class TeamCancelled(Exception):
    """The caller's cancel event fired (loop SIGTERM) while a role was running."""


def _run_with_timeout(fn: Callable[[], str], timeout_s: float, label: str,
                      stop: threading.Event | None = None,
                      cancel: threading.Event | None = None) -> str:
    """Run a role's turn on a daemon thread, bounded by `timeout_s`.

    Same shape as `tools/task.py`'s subagent timeout: nothing else on the
    call stack bounds how long a role's agent loop runs, so a stuck role
    must not hang the whole team (or the CLI). Python cannot kill a thread,
    so on a timeout or a caller cancel the role's own `stop` event is set:
    the role's `Agent` checks it between rounds and stops calling the model.
    Without that, an abandoned role kept calling Ollama after the loop had
    released its gpu-queue lease -- GPU use outside a lease.
    """
    box: dict[str, Any] = {}

    def _go() -> None:
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            box["exc"] = exc

    thread = threading.Thread(target=_go, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout_s
    while thread.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if stop is not None:
                stop.set()
            raise TimeoutError(f"{label} timed out after {timeout_s}s")
        if cancel is not None and cancel.is_set():
            if stop is not None:
                stop.set()
            raise TeamCancelled(f"{label} cancelled")
        thread.join(min(remaining, 0.5))
    if "exc" in box:
        raise box["exc"]
    return box.get("result", "")


class TeamOrchestrator:
    """Runs one team-mode task end to end."""

    def __init__(
        self,
        config: Config,
        repo_dir: str,
        *,
        approve: Callable[[str, str], Approval],
        on_event: Callable[[str, dict], None] | None = None,
        cancel: threading.Event | None = None,
        check: Callable[[str], tuple[bool | None, str]] | None = None,
        check_label: str = "task check",
    ) -> None:
        self.config = config
        self.repo_dir = repo_dir
        self.approve = approve
        self.on_event = on_event or (lambda kind, data: None)
        self.cancel = cancel or threading.Event()
        # `check` (loop mode: the queue item's test_commands) replaces the
        # project-check detection as the gate; it runs with cwd = the worktree
        # passed to it.
        self.check = check
        self.check_label = check_label
        self.role_seconds: list[tuple[str, float]] = []

    # ------------------------------------------------------------------ #

    def _agent(self, *, cwd: str, tool_names: list[str], system_extra: str,
               model: str | None = None,
               on_event: Callable[[str, dict], None] | None = None) -> Agent:
        return Agent(
            self.config, cwd, approve=self.approve,
            on_event=on_event or self.on_event,
            model=model, tool_names=tool_names, include_task=False,
            system_extra=system_extra, cancel=threading.Event(),
        )

    def _role_timeout(self) -> int:
        return getattr(self.config, "team_role_timeout_s", None) or \
            getattr(self.config, "subagent_timeout_s", 300)

    def _commit_all(self, wt_path: str, message: str) -> None:
        """Snapshot whatever a role left on disk as a real commit.

        A role's own system prompt is not reliable enough to trust it to
        remember `git commit` on its own (the same reason `verify.py` runs
        the project's check itself instead of asking the model to). Without
        this, `merge()` has nothing to bring in -- the branch tip never
        moves past its base commit, and a merge silently becomes a no-op.
        """
        subprocess.run(["git", "-C", wt_path, "add", "-A"],
                        capture_output=True, text=True)
        subprocess.run(["git", "-C", wt_path, "commit", "-q", "-m", message],
                        capture_output=True, text=True)  # no-op if nothing changed

    def _run_role(self, label: str, agent: Agent, prompt: str) -> str:
        self.on_event("team_role_start", {"role": label})
        started = time.monotonic()
        try:
            out = _run_with_timeout(
                lambda: agent.run(prompt), self._role_timeout(), label,
                stop=agent.cancel_event, cancel=self.cancel,
            )
        except TimeoutError:
            self.role_seconds.append((label, round(time.monotonic() - started, 1)))
            self.on_event("team_role_timeout", {"role": label})
            raise
        except TeamCancelled:
            self.role_seconds.append((label, round(time.monotonic() - started, 1)))
            raise
        seconds = round(time.monotonic() - started, 1)
        self.role_seconds.append((label, seconds))
        self.on_event("team_role_end", {"role": label, "output": out, "seconds": seconds})
        return out

    # ------------------------------------------------------------------ #

    def _reviewer_model(self) -> tuple[str | None, str]:
        """Which model the reviewer should use, checked against free VRAM.

        Returns (model_or_None, note). `None` means "use the main model" --
        either none was configured, or the configured one does not fit, in
        which case `note` explains the fallback so it lands in the log
        instead of silently swapping models.
        """
        reviewer_model = getattr(self.config, "reviewer_model", "") or ""
        if not reviewer_model or reviewer_model == self.config.model:
            return None, ""
        try:
            from .models import gpu_free_mb
            free_mb = gpu_free_mb(getattr(self.config, "gpu_index", 0))
        except Exception:
            free_mb = None
        if free_mb is None:
            return None, (
                f"reviewer_model {reviewer_model!r} configured but VRAM could not "
                "be checked; using the main model instead."
            )
        # No cheap way to size an arbitrary model name without a catalog
        # lookup (network + parsing); a conservative fixed floor keeps this
        # check real rather than a no-op, at the cost of being approximate.
        min_headroom_mb = getattr(self.config, "team_reviewer_min_free_mb", 8192)
        if free_mb < min_headroom_mb:
            return None, (
                f"reviewer_model {reviewer_model!r} configured but only "
                f"{free_mb}MiB free (need >= {min_headroom_mb}MiB headroom); "
                "using the main model instead."
            )
        return reviewer_model, f"reviewer using {reviewer_model!r} ({free_mb}MiB free)"

    # ------------------------------------------------------------------ #

    def plan(self, prompt: str) -> str:
        agent = self._agent(
            cwd=self.repo_dir,
            tool_names=READ_ONLY_TOOLS,
            system_extra=(
                "You are the PLANNER for a coding team. You may read the "
                "repository but must not write anything. Produce a short "
                "plan for the task below, ending with an explicit "
                "'Acceptance criteria:' bullet list that a tester can write "
                "tests from and a reviewer can check a diff against."
            ),
        )
        return self._run_role("planner", agent, prompt)

    def _gate(self, wt_path: str) -> list[GateResult]:
        # Reuses `wilbur/verify.py`'s own project-check detection/runner
        # (§16: don't grow a second copy of it here) -- `passed` is None
        # when the check could not run at all, which a gate must treat as
        # not-passing without calling it a code failure.
        if self.check is not None:
            passed, out = self.check(wt_path)
            return [GateResult(self.check_label, bool(passed), out[-4000:])]
        check = verify.detect(wt_path)
        if check is None:
            return [GateResult("no check detected", True,
                                "no project check found; nothing to gate on")]
        passed, out = verify.run(check, wt_path)
        return [GateResult(check.label, bool(passed), out[-4000:])]

    def _review(self, plan_text: str, diff_text: str,
                gates: list[GateResult], red_green: str = "") -> tuple[str, list[str]]:
        gate_summary = "\n".join(
            f"- {g.label}: {'PASS' if g.passed else 'FAIL'}" for g in gates
        )
        if red_green == "no-check":
            gate_summary += "\n- NOTE: no project check exists; nothing was run."
        model, note = self._reviewer_model()
        if note:
            self._reviewer_model_note = note
            self.on_event("team_reviewer_model", {"note": note})
        agent = self._agent(
            cwd=self.repo_dir,
            tool_names=[],
            model=model,
            system_extra=(
                "You are the REVIEWER. You see the plan, the diff, and the "
                "deterministic gate results below -- not the coder's "
                "reasoning. Judge only whether the diff satisfies the plan's "
                "acceptance criteria and the gates passed. Reply with a "
                "checklist against each acceptance criterion, then end your "
                "reply with exactly one line: 'VERDICT: APPROVE' or "
                "'VERDICT: DENY'.\n\nPLAN:\n" + plan_text +
                "\n\nGATE RESULTS:\n" + gate_summary +
                "\n\nDIFF:\n" + diff_text[:12000]
            ),
        )
        out = self._run_role("reviewer", agent,
                              "Review the change described above.")
        verdict = "DENY"
        m = re.search(r"VERDICT:\s*(APPROVE|DENY)", out, re.IGNORECASE)
        if m:
            verdict = m.group(1).upper()
        self._verdicts.append(verdict)
        return verdict, [out]

    # ------------------------------------------------------------------ #

    def run(self, prompt: str, branch: str | None = None, *, merge: bool = True) -> TeamResult:
        """`merge=False` (loop mode) stops at an approved, gate-verified branch:
        status "approved", the branch kept for the owner to review, nothing merged."""
        branch = branch or f"wilbur-team-{int(time.time())}"
        result: TeamResult | None = None
        red_green = ""
        tester_reverted: list[str] = []
        self.role_seconds = []
        self._tester_calls: list[dict] = []
        self._verdicts: list[str] = []
        self._reviewer_model_note = ""
        wt_path: str | None = None
        try:
            plan_text = self.plan(prompt)
            self.on_event("team_plan", {"plan": plan_text})

            wt_path = worktree.create(self.repo_dir, branch)

            base = subprocess.run(["git", "-C", wt_path, "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            tester_calls: list[dict] = []
            tester_role = ["tester"]

            def tester_event(kind: str, data: dict) -> None:
                # Live run 2 (2026-10-02): the tester wrote nothing and nothing
                # recorded why. Every call it makes now lands in the result.
                if kind == "tool_start":
                    tester_calls.append({"role": tester_role[0], "name": data.get("name", ""),
                                         "args": _args_summary(data.get("args")),
                                         "status": "started"})
                elif kind in ("tool_end", "tool_error"):
                    if kind == "tool_error":
                        tester_calls.append({"role": tester_role[0],
                                             "name": data.get("name", ""), "args": "",
                                             "status": "unknown-tool"})
                    else:
                        for entry in reversed(tester_calls):
                            if entry["status"] == "started" and entry["name"] == data.get("name"):
                                entry["status"] = "error" if data.get("error") else "ok"
                                break
                self.on_event(kind, data)

            self._tester_calls = tester_calls
            tester = self._agent(
                cwd=wt_path, tool_names=CODE_TOOLS, on_event=tester_event,
                system_extra=(
                    "You are the TESTER. Work from the PLAN only, not any "
                    "existing implementation. Write tests that exercise its "
                    "acceptance criteria. Write ONLY test files (under tests/ "
                    "or named test_*): do NOT implement the feature or edit any "
                    "other file -- the coder does that next, and any non-test "
                    "change you make is discarded. The feature does not exist "
                    "yet, so these tests are expected to fail until the coder "
                    "implements it.\n\nPLAN:\n" + plan_text
                ),
            )
            self._run_role("tester", tester,
                           "Write the failing tests for this task (tests only, no "
                           "implementation):\n\n" + prompt)
            tester_reverted.extend(confine_to_tests(wt_path))

            if not changed_test_paths(wt_path, base):
                tester_role[0] = "tester-retry"
                self.on_event("team_tester_retry", {"reason": "no_tests"})
                self._run_role("tester-retry", tester,
                               "You have not written any test file yet. Use write_file "
                               "now to create a test file (under tests/ or named "
                               "test_*) that tests this task and FAILS against the "
                               "current code. Do not implement the feature.")
                tester_reverted.extend(confine_to_tests(wt_path))
                if not changed_test_paths(wt_path, base):
                    result = TeamResult(
                        status="tester_no_tests", plan=plan_text,
                        summary="The tester wrote no test file, even after a re-prompt.",
                    )
                    return result
            if tester_reverted:
                self.on_event("team_tester_reverted", {"paths": tester_reverted})
            self._commit_all(wt_path, "team: tests from the plan")

            red_gates = self._gate(wt_path)
            if any(g.label == "no check detected" for g in red_gates):
                red_green = "no-check"
            elif any(not g.passed for g in red_gates):
                red_green = "red->green"
            else:
                tester_role[0] = "tester-retry"
                self.on_event("team_tester_retry", {"reason": "no_red"})
                gate_out = "\n".join(f"{g.label}: PASS\n{g.output[-1500:]}" for g in red_gates)
                self._run_role("tester-retry", tester,
                               "The gate PASSED with your tests against the current "
                               "code, so they do not test the new behaviour. Your "
                               "tests must fail against the current code. Rewrite "
                               "them so they FAIL until the feature is implemented. "
                               "Tests only, no implementation.\n\nGate output:\n"
                               + gate_out)
                more_reverted = confine_to_tests(wt_path)
                tester_reverted.extend(more_reverted)
                if more_reverted:
                    self.on_event("team_tester_reverted", {"paths": more_reverted})
                self._commit_all(wt_path, "team: tests revised to fail first")
                red_gates = self._gate(wt_path)
                if all(g.passed for g in red_gates):
                    red_green = "no-red"
                    result = TeamResult(
                        status="tester_no_red", plan=plan_text, gate_results=red_gates,
                        diff=worktree.diff(self.repo_dir, branch),
                        summary="The tester's tests pass against the unchanged code, "
                                "even after a re-prompt; nothing to go red->green.",
                    )
                    return result
                red_green = "red->green"
            self.on_event("team_gate_red", {
                "passed": [g.passed for g in red_gates], "red_green": red_green,
            })

            coder = self._agent(
                cwd=wt_path, tool_names=CODE_TOOLS,
                system_extra=(
                    "You are the CODER. Implement the PLAN below in this "
                    "worktree so its tests pass.\n\nPLAN:\n" + plan_text
                ),
            )
            self._run_role("coder", coder, prompt)
            self._commit_all(wt_path, "team: implement the plan")

            max_revisions = getattr(self.config, "team_max_revisions", 2)
            revisions = 0
            while True:
                gates = self._gate(wt_path)
                if any(not g.passed for g in gates):
                    result = TeamResult(
                        status="gate_failed", plan=plan_text, gate_results=gates,
                        diff=worktree.diff(self.repo_dir, branch),
                        revisions=revisions,
                        summary="Gates failed; the change was not sent to review.",
                    )
                    return result

                diff_text = worktree.diff(self.repo_dir, branch)
                verdict, notes = self._review(plan_text, diff_text, gates, red_green)
                self.on_event("team_review", {"verdict": verdict})

                if verdict == "APPROVE":
                    final_gates = self._gate(wt_path)
                    if any(not g.passed for g in final_gates):
                        result = TeamResult(
                            status="gate_failed", plan=plan_text,
                            gate_results=final_gates, diff=diff_text,
                            review_notes=notes, revisions=revisions,
                            summary="Gates regressed between review and merge.",
                        )
                        return result
                    if not merge:
                        result = TeamResult(
                            status="approved", plan=plan_text, diff=diff_text,
                            gate_results=final_gates, review_notes=notes,
                            revisions=revisions,
                            summary=f"Reviewer approved; gates re-verified; left on {branch}.",
                        )
                        return result
                    sha = worktree.merge(self.repo_dir, branch,
                                          message=f"team: {prompt[:60]}")
                    result = TeamResult(
                        status="merged", plan=plan_text, diff=diff_text,
                        gate_results=final_gates, review_notes=notes,
                        revisions=revisions, merge_sha=sha,
                        summary="Reviewer approved; gates re-verified and merged.",
                    )
                    return result

                revisions += 1
                if revisions > max_revisions:
                    result = TeamResult(
                        status="denied_ask_user", plan=plan_text, diff=diff_text,
                        gate_results=gates, review_notes=notes,
                        revisions=revisions,
                        summary=f"Denied {revisions} times; needs the user's call.",
                    )
                    return result

                reviser = self._agent(
                    cwd=wt_path, tool_names=CODE_TOOLS,
                    system_extra=(
                        "You are the CODER, revising after review denied the "
                        "change. Address this feedback, then stop.\n\n"
                        + "\n".join(notes) + "\n\nPLAN:\n" + plan_text
                    ),
                )
                self._run_role("coder-revise", reviser, prompt)
                self._commit_all(wt_path, "team: revise after review")
        except TimeoutError as exc:
            result = TeamResult(status="timeout", summary=str(exc))
            return result
        except TeamCancelled as exc:
            result = TeamResult(status="cancelled", summary=str(exc))
            return result
        except worktree.WorktreeError as exc:
            result = TeamResult(status="error", summary=str(exc))
            return result
        finally:
            if result is not None:
                result.role_seconds = list(self.role_seconds)
                result.red_green = result.red_green or red_green
                result.tester_reverted = tester_reverted
                result.tester_calls = list(getattr(self, "_tester_calls", []))
                result.verdicts = list(getattr(self, "_verdicts", []))
                result.reviewer_model_note = getattr(self, "_reviewer_model_note", "")
            if wt_path is not None:
                keep_branch = result is not None and result.status == "merged"
                worktree.remove(
                    self.repo_dir, wt_path,
                    branch=(branch if keep_branch else None),
                    force=(result is None or result.status != "merged"),
                )
