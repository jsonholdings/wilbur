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

_FILE_LIKE = re.compile(r"\b[\w./-]+\.[a-zA-Z]{1,5}\b")
_RISKY_LANGUAGE = re.compile(
    r"\b(refactor|refactoring|across the codebase|multiple files|"
    r"several files|rewrite|migrate|migration|redesign|restructure|"
    r"every (file|module)|throughout the)\b",
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
    status: str  # "merged" | "denied_ask_user" | "gate_failed" | "timeout" | "error"
    plan: str = ""
    diff: str = ""
    gate_results: list[GateResult] = field(default_factory=list)
    review_notes: list[str] = field(default_factory=list)
    revisions: int = 0
    merge_sha: str = ""
    summary: str = ""


def _run_with_timeout(fn: Callable[[], str], timeout_s: int, label: str) -> str:
    """Run a role's turn on a daemon thread, bounded by `timeout_s`.

    Same shape as `tools/task.py`'s subagent timeout: nothing else on the
    call stack bounds how long a role's agent loop runs, so a stuck role
    must not hang the whole team (or the CLI). A timed-out role's thread is
    abandoned, not killed -- Python has no safe way to kill a running thread.
    """
    box: dict[str, Any] = {}

    def _go() -> None:
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            box["exc"] = exc

    thread = threading.Thread(target=_go, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise TimeoutError(f"{label} timed out after {timeout_s}s")
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
    ) -> None:
        self.config = config
        self.repo_dir = repo_dir
        self.approve = approve
        self.on_event = on_event or (lambda kind, data: None)

    # ------------------------------------------------------------------ #

    def _agent(self, *, cwd: str, tool_names: list[str], system_extra: str,
               model: str | None = None) -> Agent:
        return Agent(
            self.config, cwd, approve=self.approve, on_event=self.on_event,
            model=model, tool_names=tool_names, include_task=False,
            system_extra=system_extra,
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
        try:
            out = _run_with_timeout(
                lambda: agent.run(prompt), self._role_timeout(), label,
            )
        except TimeoutError as exc:
            self.on_event("team_role_timeout", {"role": label})
            raise
        self.on_event("team_role_end", {"role": label, "output": out})
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
        check = verify.detect(wt_path)
        if check is None:
            return [GateResult("no check detected", True,
                                "no project check found; nothing to gate on")]
        passed, out = verify.run(check, wt_path)
        return [GateResult(check.label, bool(passed), out[-4000:])]

    def _review(self, plan_text: str, diff_text: str,
                gates: list[GateResult]) -> tuple[str, list[str]]:
        gate_summary = "\n".join(
            f"- {g.label}: {'PASS' if g.passed else 'FAIL'}" for g in gates
        )
        model, note = self._reviewer_model()
        if note:
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
        return verdict, [out]

    # ------------------------------------------------------------------ #

    def run(self, prompt: str, branch: str | None = None) -> TeamResult:
        branch = branch or f"wilbur-team-{int(time.time())}"
        result: TeamResult | None = None
        wt_path: str | None = None
        try:
            plan_text = self.plan(prompt)
            self.on_event("team_plan", {"plan": plan_text})

            wt_path = worktree.create(self.repo_dir, branch)

            tester = self._agent(
                cwd=wt_path, tool_names=CODE_TOOLS,
                system_extra=(
                    "You are the TESTER. Work from the PLAN only, not any "
                    "existing implementation. Write tests that exercise its "
                    "acceptance criteria. The feature does not exist yet, so "
                    "these tests are expected to fail until the coder "
                    "implements it.\n\nPLAN:\n" + plan_text
                ),
            )
            self._run_role("tester", tester, prompt)
            self._commit_all(wt_path, "team: tests from the plan")

            red_gates = self._gate(wt_path)
            self.on_event("team_gate_red", {
                "passed": [g.passed for g in red_gates],
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
                verdict, notes = self._review(plan_text, diff_text, gates)
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
        except worktree.WorktreeError as exc:
            result = TeamResult(status="error", summary=str(exc))
            return result
        finally:
            if wt_path is not None:
                keep_branch = result is not None and result.status == "merged"
                worktree.remove(
                    self.repo_dir, wt_path,
                    branch=(branch if keep_branch else None),
                    force=(result is None or result.status != "merged"),
                )
