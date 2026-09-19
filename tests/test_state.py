"""The harness must not be able to forget the objective or the plan."""
from __future__ import annotations

from wilbur.context import compact
from wilbur.state import BRIEFING_MARKER, RunState


class _Cfg:
    num_ctx = 8192
    compact_at = 0.75
    reserve_output = 512
    model = "m"


class _Reply:
    def __init__(self, content): self.content = content


class _Client:
    """A summariser that paraphrases, which is exactly the hazard."""
    def __init__(self): self.calls = 0
    def chat(self, *a, **k):
        self.calls += 1
        return _Reply("Earlier the user asked about SOMETHING ELSE entirely.")


def _long_history(n=60):
    msgs = [{"role": "system", "content": "sys"}]
    msgs.append({"role": "user", "content": "THE REAL OBJECTIVE: rename foo to bar"})
    for i in range(n):
        msgs.append({"role": "assistant", "content": f"step {i} " + "x" * 400})
        msgs.append({"role": "user", "content": f"ok {i} " + "y" * 400})
    return msgs


def test_briefing_survives_compaction():
    """The whole point. A pinned briefing must come out the other side
    verbatim, not summarised into a paraphrase."""
    st = RunState(objective="THE REAL OBJECTIVE: rename foo to bar")
    msgs = _long_history()
    msgs.insert(1, {"role": "system", "content": st.briefing()})

    out = compact(msgs, _Client(), _Cfg())

    pinned = [m for m in out if m["role"] == "system"
              and BRIEFING_MARKER in (m.get("content") or "")]
    assert len(pinned) == 1, "the briefing was dropped or duplicated by compaction"
    assert "rename foo to bar" in pinned[0]["content"]


def test_without_pinning_the_objective_is_lost():
    """Proves the defect this feature fixes is real, rather than asserting the
    fix works against a hazard that never existed."""
    msgs = _long_history()
    out = compact(msgs, _Client(), _Cfg())
    surviving = " ".join(m.get("content") or "" for m in out)
    assert "THE REAL OBJECTIVE" not in surviving, (
        "the unpinned objective survived, so this test no longer proves anything"
    )


def test_plan_is_rendered_with_progress_and_current_item():
    st = RunState(objective="o")
    st.set_plan([{"task": "one", "status": "completed"},
                 {"task": "two", "status": "in_progress"},
                 {"task": "three"}])
    text = st.briefing()
    assert "1/3 complete" in text
    assert "Current item: two" in text
    assert st.progress == (1, 3)


def test_plan_accepts_the_todo_tools_key():
    """todo_write emits {"task": ...}; state must not silently drop them."""
    st = RunState()
    st.set_plan([{"task": "real"}, {"nothing": "useful"}])
    assert [i.text for i in st.plan] == ["real"]


def test_failure_ledger_counts_repeats_not_duplicates():
    st = RunState()
    st.record_failure("run_bash(command=pytest)", "exit 1")
    st.record_failure("run_bash(command=pytest)", "exit 1 again")
    assert len(st.failures) == 1
    assert st.repeated("run_bash(command=pytest)") == 2
    assert "attempted 2 times" in st.briefing()


def test_failure_ledger_is_bounded():
    st = RunState()
    for i in range(50):
        st.record_failure(f"call-{i}", "boom")
    assert len(st.failures) <= 6
    assert st.failures[-1].what == "call-49", "the ledger kept the oldest, not the newest"


def test_briefing_is_none_when_there_is_nothing_to_say():
    assert RunState().briefing() is None


def test_state_round_trips_through_json():
    st = RunState(objective="o")
    st.set_plan([{"task": "a", "status": "completed"}])
    st.record_failure("x", "y")
    back = RunState.from_dict(st.to_dict())
    assert back.objective == "o"
    assert back.plan[0].text == "a" and back.plan[0].done
    assert back.repeated("x") == 1
