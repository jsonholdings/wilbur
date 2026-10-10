"""RunState plan/failure behaviour and dict round-tripping.

Complements tests/test_state.py: that file checks the briefing rendering and
the ledger bounds; here we check the accessors directly and that
to_dict()/from_dict() preserve the full state, including failure counts.
"""
from __future__ import annotations

from wilbur.state import RunState


def test_current_prefers_in_progress_then_first_pending():
    st = RunState()
    st.set_plan([{"task": "a", "status": "pending"},
                 {"task": "b", "status": "in_progress"},
                 {"task": "c", "status": "pending"}])
    assert st.current is not None
    assert st.current.text == "b", "in_progress must win over pending"
    assert st.progress == (0, 3)

    st.set_plan([{"task": "a", "status": "completed"},
                 {"task": "b", "status": "completed"}])
    assert st.current is None, "an all-completed plan has no current item"
    assert st.progress == (2, 2)


def test_repeated_counts_only_matching_what_and_strips_whitespace():
    st = RunState()
    st.record_failure("  run_bash(command=pytest)  ", "exit 1")
    st.record_failure("  run_bash(command=pytest)  ", "exit 1 again")
    st.record_failure("other", "boom")
    assert st.repeated("run_bash(command=pytest)") == 2
    assert st.repeated("other") == 1
    assert st.repeated("never-tried") == 0


def test_roundtrip_preserves_plan_failures_and_counters():
    st = RunState(objective="rename foo to bar")
    st.set_plan([{"task": "a", "status": "completed"},
                 {"task": "b", "status": "in_progress"},
                 {"task": "c"}])
    st.record_failure("pytest", "exit 1")
    st.record_failure("pytest", "exit 1 again")
    st.record_verification("pytest passed")
    st.writes = 4
    st.last_tool = "edit_file"

    back = RunState.from_dict(st.to_dict())

    assert back.to_dict() == st.to_dict(), "round-trip must be lossless"
    assert back.objective == "rename foo to bar"
    assert [i.text for i in back.plan] == ["a", "b", "c"]
    assert [i.status for i in back.plan] == ["completed", "in_progress", "pending"]
    assert back.progress == (1, 3)
    assert back.current is not None and back.current.text == "b"
    assert back.repeated("pytest") == 2
    assert back.failures[0].why == "exit 1 again"
    assert back.verifications == ["pytest passed"]
    assert back.writes == 4
    assert back.last_tool == "edit_file"


def test_roundtrip_of_empty_state_is_empty():
    back = RunState.from_dict(RunState().to_dict())
    assert back.plan == []
    assert back.failures == []
    assert back.progress == (0, 0)
    assert back.current is None
    assert back.repeated("anything") == 0
