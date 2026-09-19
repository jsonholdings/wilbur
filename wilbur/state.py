"""Run state the harness owns, not the conversation.

The failure this module exists to prevent: on a long job the model stops working
on what it was asked and starts working on a summary of what it was asked.

`compact()` replaces the middle of the history with a paraphrase. The system
prompt survives and the recent tail survives -- but the user's actual
instruction is the *first user message*, which sits in the middle and gets
paraphrased along with everything else. So does the plan, because `todo_write`
writes into the transcript like any other tool.

Both belong to the harness instead. The model proposes; the harness holds, and
re-renders a briefing into context every turn. That way the objective is
verbatim on turn 60, not a summary of a summary, and the plan cannot be
compacted away from underneath the agent working through it.

None of this makes the model smarter. It removes the need for it to remember.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

BRIEFING_MARKER = "[wilbur:briefing]"

# A ledger longer than this stops being a warning and starts being context
# pressure; the oldest failures are the least relevant to what to try next.
MAX_FAILURES = 6
MAX_VERIFICATIONS = 3


@dataclass
class PlanItem:
    text: str
    status: str = "pending"          # pending | in_progress | completed

    @property
    def done(self) -> bool:
        return self.status == "completed"


@dataclass
class Failure:
    what: str                        # what was attempted
    why: str                         # how it failed
    count: int = 1


@dataclass
class RunState:
    """Everything the agent must not be allowed to forget."""
    objective: str = ""
    plan: list[PlanItem] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    verifications: list[str] = field(default_factory=list)

    # -- plan -------------------------------------------------------------
    def set_plan(self, items: list[dict[str, Any]]) -> None:
        self.plan = [PlanItem(text=str(i.get("text") or i.get("task") or "").strip(),
                              status=str(i.get("status") or "pending"))
                     for i in items if (i.get("text") or i.get("task"))]

    @property
    def current(self) -> PlanItem | None:
        for item in self.plan:
            if item.status == "in_progress":
                return item
        for item in self.plan:
            if item.status == "pending":
                return item
        return None

    @property
    def progress(self) -> tuple[int, int]:
        return sum(1 for i in self.plan if i.done), len(self.plan)

    # -- failure ledger ---------------------------------------------------
    def record_failure(self, what: str, why: str) -> None:
        """Remember a dead end.

        A local model that has just seen an error will cheerfully retry the
        identical command. Counting repeats is what lets the briefing say "you
        have tried this twice" rather than silently letting it happen a third
        time.
        """
        what, why = what.strip(), " ".join(why.split())[:200]
        for f in self.failures:
            if f.what == what:
                f.count += 1
                f.why = why
                return
        self.failures.append(Failure(what=what, why=why))
        if len(self.failures) > MAX_FAILURES:
            del self.failures[0]

    def repeated(self, what: str) -> int:
        for f in self.failures:
            if f.what == what.strip():
                return f.count
        return 0

    def record_verification(self, line: str) -> None:
        self.verifications.append(line)
        if len(self.verifications) > MAX_VERIFICATIONS:
            del self.verifications[0]

    # -- rendering --------------------------------------------------------
    def briefing(self) -> str | None:
        """The block re-injected each turn. None when there is nothing to say."""
        if not (self.objective or self.plan or self.failures):
            return None
        out = [BRIEFING_MARKER,
               "This block is maintained by the harness and is always current. "
               "It is not part of the conversation and is never summarised."]

        if self.objective:
            out += ["", "OBJECTIVE (verbatim, as the user wrote it):",
                    self.objective.strip()]

        if self.plan:
            done, total = self.progress
            out += ["", f"PLAN ({done}/{total} complete):"]
            for i, item in enumerate(self.plan, 1):
                mark = {"completed": "x", "in_progress": ">"}.get(item.status, " ")
                out.append(f"  [{mark}] {i}. {item.text}")
            cur = self.current
            if cur:
                out.append(f"Current item: {cur.text}")
            elif total:
                out.append("Every plan item is complete. Verify, then report and stop.")

        if self.failures:
            out += ["", "ALREADY TRIED AND FAILED -- do not repeat these:"]
            for f in self.failures:
                again = f" (attempted {f.count} times)" if f.count > 1 else ""
                out.append(f"  - {f.what}{again}: {f.why}")

        if self.verifications:
            out += ["", "LAST VERIFICATION:"] + [f"  {v}" for v in self.verifications]

        return "\n".join(out)

    # -- persistence ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        st = cls(objective=data.get("objective", ""))
        st.plan = [PlanItem(**i) for i in data.get("plan", [])]
        st.failures = [Failure(**f) for f in data.get("failures", [])]
        st.verifications = list(data.get("verifications", []))
        return st
