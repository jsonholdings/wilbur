"""Opt-in critic pass: reviews each write with one extra model call."""
from wilbur.fake_backend import Reply
from wilbur.critic import review
from wilbur.config import Config

from conftest import call, say


def _write_turn():
    return Reply(content="", tool_calls=[call("write_file", path="a.py", content="x = 1\n")])


def _tool_results(client):
    return [m.get("content", "") for m in client.seen[-1] if m.get("role") == "tool"]


def test_off_by_default_makes_no_extra_call(make_agent):
    agent, client = make_agent([_write_turn(), say("done")])
    assert agent.run("write a.py") == "done"
    assert len(client.seen) == 2
    assert not any("critic" in t for t in _tool_results(client))


def test_problem_verdict_is_appended_to_the_tool_result(make_agent):
    agent, client = make_agent(
        [_write_turn(), say("PROBLEM: x is never used."), say("done")], critic_pass=True)
    assert agent.run("write a.py") == "done"
    assert len(client.seen) == 3
    assert any("critic flagged" in t and "never used" in t for t in _tool_results(client))


def test_ok_verdict_adds_nothing(make_agent):
    agent, client = make_agent([_write_turn(), say("OK"), say("done")], critic_pass=True)
    assert agent.run("write a.py") == "done"
    assert not any("critic" in t for t in _tool_results(client))


def test_critic_failure_never_fails_the_write():
    class Broken:
        def chat(self, *a, **kw):
            raise ConnectionError("down")
    assert review(Broken(), Config(), "edit_file", {"path": "a"}, "ok") is None
