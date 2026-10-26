"""Long-task robustness (2.40.0): the four guards that stop an abliterated
thinking model from retrying, spinning out, or returning a mountain of text
on a long task.

1. empty-reply recovery (nudge, then surface a short summary of the reasoning
   instead of hard-stopping a task that is still working),
2. tool-failure circuit breaker (hard-block an identical failing call; stop
   cleanly after too many consecutive failures),
3. runaway-output cap (a wall of prose two turns running is truncated + nudged),
4. long-task continuity is covered in test_keepgoing.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur.llm import Reply
from conftest import say, call


def _events(agent):
    seen = []
    agent.on_event = lambda kind, data: seen.append((kind, data))
    return seen


# --------------------------------------------------------------------- #
# 1. empty-reply recovery

def test_empty_reply_retries_are_configurable_and_recover(make_agent):
    # Two empty replies are nudged (empty_reply_retries=2), then the model
    # answers -- the task is not stopped.
    a, _ = make_agent([say(""), say(""), say("the answer")],
                      empty_reply_retries=2)
    seen = _events(a)
    assert a.run("go") == "the answer"
    assert sum(1 for k, _ in seen if k == "empty_reply") == 2


def test_empty_give_up_with_thinking_surfaces_a_short_summary(make_agent):
    # Retries spent and the reply is still empty, but the model DID reason:
    # surface a truncated summary (not the wall) and do NOT hard-stop. The
    # first all-reasoning reply is asked again with thinking off (2.43.0);
    # this one stays empty even then.
    big = "reasoning " * 500          # > 1500 chars
    thought = Reply(content="", tool_calls=[], thinking=big)
    a, _ = make_agent([say(""), thought, thought], empty_reply_retries=1)
    seen = _events(a)
    final = a.run("go")
    assert "(reasoning truncated)" in final
    assert len(final) < len(big)                 # truncated, not the whole wall
    assert a.last_reply_empty is False           # keep-going may carry on
    assert any(k == "empty_reply_recovered" for k, _ in seen)


def test_truly_empty_after_retries_stops_the_run(make_agent):
    # No content and no reasoning after the retries: this one does stop.
    a, _ = make_agent([say(""), say("")], empty_reply_retries=1)
    assert a.run("go") == ""
    assert a.last_reply_empty is True


def test_empty_counter_resets_on_a_real_reply(make_agent):
    # An empty reply followed by a real one, then another empty much later,
    # must each get their own retry budget -- the counter is not sticky.
    a, _ = make_agent([say(""), call_reply("read_file", path="x"), say(""), say("done")],
                      empty_reply_retries=1)
    # read_file on a missing path errors, but it is a non-empty (tool) reply
    # that resets the empty counter; the later empty then gets its own nudge.
    assert a.run("go") == "done"


def call_reply(name, **args):
    return Reply(content="", tool_calls=[call(name, **args)])


# --------------------------------------------------------------------- #
# 2. tool-failure circuit breaker

def test_identical_failing_call_is_hard_blocked_after_N(make_agent):
    # read_file on a missing path fails every time. After tool_failure_block_at
    # failures the harness refuses to run the identical call again.
    calls = [call_reply("read_file", path="missing.txt") for _ in range(4)]
    a, _ = make_agent(calls + [say("ok")],
                      tool_failure_block_at=3, tool_failure_stop_at=99)
    seen = _events(a)
    assert a.run("go") == "ok"
    assert any(k == "tool_blocked" for k, _ in seen), "the 4th identical call was not blocked"
    tool_msgs = [m.get("content") or "" for m in a.messages if m.get("role") == "tool"]
    assert any("[harness] blocked" in m for m in tool_msgs)


def test_run_stops_cleanly_after_M_consecutive_failures(make_agent):
    # Distinct failing calls (so the per-call block never fires) accumulate
    # consecutive failures; at tool_failure_stop_at the run stops with a useful
    # summary rather than spinning to the round cap.
    calls = [call_reply("read_file", path=f"missing{i}.txt") for i in range(4)]
    a, _ = make_agent(calls + [say("should not reach")],
                      tool_failure_block_at=99, tool_failure_stop_at=4, max_turns=20)
    seen = _events(a)
    final = a.run("go")
    assert a.tool_circuit_tripped is True
    assert final.startswith("stuck:") and "Next step:" in final
    assert any(k == "tool_circuit_stop" for k, _ in seen)


def test_a_successful_write_resets_the_consecutive_failure_counter(make_agent):
    # A good write between failures proves progress, so the counter resets and
    # the circuit breaker does not trip.
    a, _ = make_agent([
        call_reply("read_file", path="missA.txt"),
        call_reply("read_file", path="missB.txt"),
        Reply(content="", tool_calls=[call("write_file", path="ok.txt", content="x")]),
        call_reply("read_file", path="missC.txt"),
        say("done"),
    ], tool_failure_block_at=99, tool_failure_stop_at=3)
    assert a.run("go") == "done"
    assert a.tool_circuit_tripped is False


# --------------------------------------------------------------------- #
# 3. runaway-output cap

def test_runaway_output_is_truncated_and_nudged_on_the_second_turn(make_agent):
    # Streak persists across run() calls (keep-going drives the recurrence).
    # The first wall of prose is allowed through; a second one running is
    # truncated in the transcript and nudged for a concrete next action.
    a, client = make_agent([say("A" * 400), say("B" * 400), say("ok now acting")],
                           runaway_content_chars=100)
    seen = _events(a)
    assert a.run("go") == "A" * 400          # first offense surfaced
    final = a.run("continue")                # second wall -> truncate + nudge, then act
    assert final == "ok now acting"
    assert any(k == "runaway_output" for k, _ in seen)
    assert any("output truncated by harness" in (m.get("content") or "")
               for m in a.messages if m.get("role") == "assistant")
