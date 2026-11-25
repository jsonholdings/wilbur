"""2.56.0 (parity G7, BACKLOG loop-breaker-carryover): each `wilbur loop` /
goal-loop turn starts with a fresh tool-failure budget, as a typed REPL line
does since 2.51.1, so a trip in one turn does not re-trip the next turn on its
first failing call."""
from __future__ import annotations

import wilbur.agent as agent_mod
from wilbur import loop
from wilbur.llm import Reply

from conftest import call, say

DONE = say(f"Recovered with a different approach.\n{loop.GOAL_COMPLETE}")


def _miss(i: int) -> Reply:
    return Reply(content="", tool_calls=[call("read_file", path=f"missing{i}.txt")])


def _agent(make_agent):
    a, _ = make_agent([_miss(0), _miss(1), _miss(2),   # turn 1 trips at 3
                       _miss(3), DONE],                # turn 2: one miss, then done
                      tool_failure_block_at=99, tool_failure_stop_at=3, max_turns=20)
    return a


def test_next_loop_turn_gets_a_fresh_failure_budget(make_agent):
    a = _agent(make_agent)
    result = loop.run_goal_loop(a, "g", max_turns=2)
    assert result.status == "unverified" and result.turns == 2
    assert a.tool_circuit_tripped is False


def test_control_without_the_reset_turn_two_re_trips(monkeypatch, make_agent):
    """Control: the carried-over count trips turn 2 on its first failure."""
    monkeypatch.setattr(agent_mod.Agent, "begin_user_turn", lambda self: None)
    a = _agent(make_agent)
    result = loop.run_goal_loop(a, "g", max_turns=2)
    assert result.status == "turn_cap"
    assert result.final.startswith("stuck:")
