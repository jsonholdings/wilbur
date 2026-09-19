"""The subagent tool: dispatch, isolation, approval, and failure handling.

A FakeAgent stands in for the real loop so these exercise task.py's own logic
-- argument handling, which tools each agent_type gets, the approval gate, and
how a subagent's report and touched-files footer come back -- without a model.
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import wilbur.agent as agent_mod
from wilbur.config import Config
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.task import AGENT_TYPES, Task


class FakeSub:
    """Records how it was constructed; returns a canned report."""
    last = None

    def __init__(self, **kw):
        self.kw = kw
        self.report = "did the thing"
        self.raise_exc = None
        self.ctx = type("C", (), {"read_files": {}})()
        FakeSub.last = self

    def run(self, prompt):
        self.prompt = prompt
        if self.raise_exc:
            raise self.raise_exc
        return self.report


@pytest.fixture
def ctx(tmp_path):
    approvals = []
    def approve(tool, detail):
        approvals.append((tool, detail))
        return Approval.GRANTED if ctx_obj.allow else Approval.DENIED
    ctx_obj = ToolContext(cwd=str(tmp_path), config=Config(),
                          read_files={}, approve=approve)
    ctx_obj.allow = True
    ctx_obj.approvals = approvals
    return ctx_obj


@pytest.fixture(autouse=True)
def patch_agent(monkeypatch):
    monkeypatch.setattr(agent_mod, "Agent", FakeSub)
    FakeSub.last = None


def run_task(ctx, **args):
    return Task().run(args, ctx)


# --------------------------------------------------------------------- #
# argument handling


def test_empty_prompt_is_rejected(ctx):
    res = run_task(ctx, prompt="  ")
    assert res.is_error and "requires a prompt" in res.output


def test_unknown_agent_type_lists_valid_ones(ctx):
    res = run_task(ctx, prompt="find x", agent_type="wizard")
    assert res.is_error
    assert "explore" in res.output and "general" in res.output


def test_subagent_gets_its_own_round_budget_not_the_parents(ctx):
    """A subagent used to inherit the parent's `max_turns` verbatim, so a
    parent configured for a long task gave a runaway subagent the same huge
    budget -- it could burn the wall-clock timeout without ever hitting a
    round limit that would make it stop and summarise. It must get its own
    `config.subagent_max_turns`, and the parent's own Config object must be
    untouched (a subagent must never be able to change the parent's budget)."""
    ctx.config.max_turns = 60
    ctx.config.subagent_max_turns = 7
    run_task(ctx, prompt="find x", agent_type="explore")
    assert FakeSub.last.kw["config"].max_turns == 7
    assert FakeSub.last.kw["config"] is not ctx.config
    assert ctx.config.max_turns == 60, "the parent's own config must not be mutated"


def test_default_agent_type_is_explore(ctx):
    run_task(ctx, prompt="find the config loader")
    assert FakeSub.last.kw["tool_names"] == AGENT_TYPES["explore"]["tools"]


# --------------------------------------------------------------------- #
# isolation guarantees


def test_explore_gets_only_read_tools_and_no_nesting(ctx):
    run_task(ctx, prompt="search", agent_type="explore")
    kw = FakeSub.last.kw
    assert "run_bash" in kw["tool_names"]        # explore may shell out
    assert "write_file" not in kw["tool_names"]  # but cannot write
    assert kw["include_task"] is False           # subagents never nest


def test_general_gets_all_tools(ctx):
    run_task(ctx, prompt="do work", agent_type="general")
    assert FakeSub.last.kw["tool_names"] is None  # None = full registry


# --------------------------------------------------------------------- #
# approval gate


def test_explore_is_read_only_and_skips_approval(ctx):
    run_task(ctx, prompt="search", agent_type="explore")
    assert ctx.approvals == []


def test_general_requires_approval(ctx):
    run_task(ctx, prompt="work", agent_type="general")
    assert any(t == "task" for t, _ in ctx.approvals)


def test_general_denied_does_not_run_the_subagent(ctx):
    ctx.allow = False
    res = run_task(ctx, prompt="work", agent_type="general")
    assert res.is_error and "denied" in res.output.lower()
    assert FakeSub.last is None  # approval is checked before the agent is built


# --------------------------------------------------------------------- #
# reporting


def test_successful_report_is_returned(ctx):
    res = run_task(ctx, prompt="search", agent_type="explore")
    assert not res.is_error and "did the thing" in res.output


# --------------------------------------------------------------------- #
# hang protection: a subagent that never returns must not hang the CLI


def test_stuck_subagent_times_out_instead_of_hanging(ctx):
    """Owner report 2026-09-18: "the wilbur cli spawns agents and never
    returns". sub.run() is called synchronously inside this tool call with
    no other bound on it, so a subagent stuck in a slow/CPU-spilled model
    loop hangs the whole process. task.py now runs it on a thread and joins
    with config.subagent_timeout_s -- this proves the call returns control
    (an error ToolResult) within that budget instead of blocking forever."""
    class HangingSub(FakeSub):
        def run(self, prompt):
            time.sleep(5)  # far longer than the test's configured timeout
            return "too late"

    import wilbur.agent as am
    am.Agent = HangingSub
    ctx.config.subagent_timeout_s = 0.2

    started = time.time()
    res = run_task(ctx, prompt="work", agent_type="general")
    elapsed = time.time() - started

    assert elapsed < 2.0, f"task.run() blocked for {elapsed}s past its timeout"
    assert res.is_error
    assert "timed out" in res.output.lower()


def test_subagent_exception_is_still_reported_not_swallowed(ctx):
    def build(**kw):
        s = FakeSub(**kw)
        s.raise_exc = RuntimeError("boom")
        return s
    import wilbur.agent as am
    am.Agent = build
    res = run_task(ctx, prompt="work", agent_type="general")
    assert res.is_error and "boom" in res.output


def test_touched_files_footer_is_appended(ctx):
    task = Task()
    # make the fake subagent report having read two files
    def build(**kw):
        s = FakeSub(**kw)
        s.ctx.read_files = {"a.py": 1, "b.py": 1}
        return s
    import wilbur.agent as am
    am.Agent = build
    res = task.run({"prompt": "search", "agent_type": "explore"}, ctx)
    assert "[subagent read: a.py, b.py]" in res.output


def test_empty_report_is_an_error(ctx):
    def build(**kw):
        s = FakeSub(**kw); s.report = "   "
        return s
    import wilbur.agent as am
    am.Agent = build
    res = Task().run({"prompt": "search", "agent_type": "explore"}, ctx)
    assert res.is_error and "no report" in res.output.lower()


def test_subagent_exception_is_caught(ctx):
    def build(**kw):
        s = FakeSub(**kw); s.raise_exc = RuntimeError("model crashed")
        return s
    import wilbur.agent as am
    am.Agent = build
    res = Task().run({"prompt": "search", "agent_type": "explore"}, ctx)
    assert res.is_error and "Subagent failed: RuntimeError" in res.output


def test_subagent_receives_the_objective_as_context_not_as_its_task(monkeypatch, tmp_path):
    """A delegated worker with no view of the larger goal optimises for its
    slice -- renaming a symbol its caller needed, or 'fixing' the test it was
    meant to make pass. It gets the goal as context; its task stays its prompt.
    """
    import wilbur.tools.task as task_mod
    from wilbur.state import RunState
    from wilbur.tools.base import Approval, ToolContext
    from wilbur.config import Config

    captured = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured.update(kw)
            self.ctx = type("C", (), {"read_files": {}})()
        def run(self, prompt):
            captured["prompt"] = prompt
            return "report"

    monkeypatch.setattr("wilbur.agent.Agent", FakeAgent)

    parent = RunState(objective="Migrate the whole billing module to the new API")
    parent.set_plan([{"task": "port the invoice writer", "status": "in_progress"}])
    ctx = ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                      approve=lambda *_: Approval.GRANTED, state=parent)

    task_mod.Task().run({"prompt": "find every call site", "agent_type": "explore"}, ctx)

    extra = captured["system_extra"]
    assert "Migrate the whole billing module" in extra, "objective was not passed as context"
    assert "port the invoice writer" in extra, "caller's current plan item was not passed"
    assert "Do NOT take this on yourself" in extra, "context was not marked as context"
    assert captured["prompt"] == "find every call site", "the slice stopped being the task"
    # Its own plan and ledger, not the caller's -- a subagent marking the
    # caller's plan complete would be a lie the caller then acts on.
    assert captured["state"] is not parent
    assert captured["state"].plan == []


def test_subagent_without_parent_state_still_runs(monkeypatch, tmp_path):
    """Tools must be exercisable without standing up a whole agent."""
    import wilbur.tools.task as task_mod
    from wilbur.tools.base import Approval, ToolContext
    from wilbur.config import Config

    class FakeAgent:
        def __init__(self, **kw): self.ctx = type("C", (), {"read_files": {}})()
        def run(self, prompt): return "report"

    monkeypatch.setattr("wilbur.agent.Agent", FakeAgent)
    ctx = ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                      approve=lambda *_: Approval.GRANTED)
    result = task_mod.Task().run({"prompt": "x", "agent_type": "explore"}, ctx)
    assert not result.is_error


# --------------------------------------------------------------------- #
# /agents registry


@pytest.fixture(autouse=True)
def clear_agents_registry():
    from wilbur.tools import agents_registry
    agents_registry.clear()
    yield
    agents_registry.clear()


def test_successful_run_registers_and_finishes_as_done(ctx):
    from wilbur.tools import agents_registry
    run_task(ctx, prompt="search", agent_type="explore", description="my search")
    records = agents_registry.snapshot()
    assert len(records) == 1
    assert records[0].status == "done"
    assert records[0].label == "my search"
    assert records[0].kind == "explore"
    assert agents_registry.running() == []


def test_timed_out_subagent_is_recorded_as_timeout(ctx):
    from wilbur.tools import agents_registry

    class HangingSub(FakeSub):
        def run(self, prompt):
            time.sleep(5)
            return "too late"

    import wilbur.agent as am
    am.Agent = HangingSub
    ctx.config.subagent_timeout_s = 0.2
    run_task(ctx, prompt="work", agent_type="general")
    records = agents_registry.snapshot()
    assert records[0].status == "timeout"


def test_failed_subagent_is_recorded_as_error(ctx):
    from wilbur.tools import agents_registry

    def build(**kw):
        s = FakeSub(**kw)
        s.raise_exc = RuntimeError("boom")
        return s
    import wilbur.agent as am
    am.Agent = build
    run_task(ctx, prompt="work", agent_type="general")
    records = agents_registry.snapshot()
    assert records[0].status == "error"


# --------------------------------------------------------------------- #
# Ctrl-C cancels a subagent cooperatively


def test_keyboard_interrupt_during_join_sets_cancel_and_marks_cancelled(ctx):
    """A Ctrl-C while Task.run() is blocked joining the subagent thread must
    set the subagent's cancel flag (visible on the Agent it built) and
    record the registry entry as cancelled, then re-raise so the REPL's own
    KeyboardInterrupt handling still runs."""
    class SlowSub(FakeSub):
        def run(self, prompt):
            # Give the main thread time to be "in" the join() call before
            # this returns, without actually depending on real timing for
            # correctness -- the assertion is on what task.py did with the
            # cancel flag and the registry, not on scheduling.
            time.sleep(0.3)
            return "done" if not self.cancel_event_checked else "n/a"
        cancel_event_checked = False

    import wilbur.agent as am
    am.Agent = SlowSub

    from wilbur.tools import agents_registry

    real_join = threading.Thread.join
    calls = {"n": 0}

    def fake_join(self, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return real_join(self, timeout)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(threading.Thread, "join", fake_join)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_task(ctx, prompt="work", agent_type="general")
    finally:
        monkeypatch.undo()

    assert FakeSub.last.kw["cancel"].is_set()
    records = agents_registry.snapshot()
    assert records[0].status == "cancelled"
