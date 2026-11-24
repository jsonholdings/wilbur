"""A tripped tool-failure breaker resets when the user types a new message
(2.51.1). Owner, 2026-11-23 ~21:35: after the breaker tripped, "/continue",
"fix the tool issue and then try again" and "can you not install the missing
module?" each tripped it again on the model's first failing call (counter
7, 8, 9), because consecutive_tool_failures carried over between user turns.
The stop message promises "a fresh budget"; this holds Wilbur to it."""
from __future__ import annotations

from wilbur.llm import Reply

from conftest import call, say
from test_keepgoing import _run


def _miss(i: int) -> Reply:
    return Reply(content="", tool_calls=[call("read_file", path=f"missing{i}.txt")])


def _limits(cfg):
    cfg.tool_failure_block_at = 99
    cfg.tool_failure_stop_at = 3


def test_new_user_message_gets_a_fresh_failure_budget(make_agent):
    a, _ = make_agent([_miss(0), _miss(1), _miss(2),          # trips at 3
                       _miss(3), say("recovered: different approach worked")],
                      tool_failure_block_at=99, tool_failure_stop_at=3, max_turns=20)
    assert a.run("go").startswith("stuck:")
    assert a.tool_circuit_tripped is True
    a.begin_user_turn()                       # what the REPL does for a typed line
    assert a.run("continue") == "recovered: different approach worked"
    assert a.tool_circuit_tripped is False


def test_control_without_the_reset_the_next_failure_re_trips(make_agent):
    """Control: the incident shape. Skip begin_user_turn and the carried-over
    count trips the breaker on the first failing call of the next message."""
    a, _ = make_agent([_miss(0), _miss(1), _miss(2), _miss(3), say("unreached")],
                      tool_failure_block_at=99, tool_failure_stop_at=3, max_turns=20)
    a.run("go")
    assert a.run("continue").startswith("stuck:")


def test_repl_resets_the_budget_for_each_typed_line(monkeypatch, tmp_path):
    import wilbur.agent as agent_mod
    calls = []
    real = agent_mod.Agent.begin_user_turn
    monkeypatch.setattr(agent_mod.Agent, "begin_user_turn",
                        lambda self: (calls.append(1), real(self))[1])
    _run(monkeypatch, tmp_path, [say("hi")], lines=("go",))
    assert calls == [1]


def test_breaker_still_trips_within_one_user_turn(monkeypatch, tmp_path, capsys):
    """Control: the reset is per user message, not per round, so a single turn
    of failures still stops at the limit."""
    replies = [_miss(i) for i in range(6)] + [say("should not reach")]
    _run(monkeypatch, tmp_path, replies, lines=("go",), configure=_limits)
    out = capsys.readouterr().out
    assert "tool-failure circuit breaker" in out
    assert "should not reach" not in out
