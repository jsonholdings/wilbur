"""The agent loop: tool-result pairing, failure isolation, turn limits.

The loop iterates over every tool call in a reply, so a turn can carry more than
one. Results must be labelled with the tool that produced them or the model has
to infer the pairing from order.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import wilbur.agent as agent_mod
from wilbur.agent import Agent
from wilbur.config import Config
from wilbur.llm import Reply, ToolCall
from wilbur.tools.base import Approval


class FakeClient:
    """Returns queued replies, and records the messages it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, *a, **kw):
        return self

    def chat(self, messages, schema, **kw):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else Reply(content="done", tool_calls=[])


@pytest.fixture
def make_agent(monkeypatch, tmp_path):
    def build(replies, **cfg_kw):
        client = FakeClient(replies)
        monkeypatch.setattr(agent_mod, "OllamaClient", client)
        config = Config()
        config.max_turns = cfg_kw.pop("max_turns", 10)
        for k, v in cfg_kw.items():
            setattr(config, k, v)
        a = Agent(config, str(tmp_path), approve=lambda *_: Approval.GRANTED)
        return a, client
    return build


def say(text):
    return Reply(content=text, tool_calls=[])


def call(name, **args):
    return ToolCall(name=name, arguments=args)


# --------------------------------------------------------------------- #
# tool-result pairing


def test_tool_result_carries_the_tool_name():
    msg = Agent._tool_message(call("read_file", path="x"), "contents")
    assert msg["role"] == "tool"
    assert msg["tool_name"] == "read_file"
    assert msg["content"] == "contents"


def test_tool_result_omits_call_id_when_the_model_gave_none():
    assert "tool_call_id" not in Agent._tool_message(call("grep"), "out")


def test_tool_result_keeps_the_call_id_when_present():
    c = ToolCall(name="grep", arguments={}, call_id="abc123")
    assert Agent._tool_message(c, "out")["tool_call_id"] == "abc123"


def test_two_calls_in_one_turn_get_distinguishable_results(make_agent, tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "b.txt").write_text("beta\n")
    replies = [
        Reply(content="", tool_calls=[call("read_file", path="a.txt"),
                                      call("read_file", path="b.txt")]),
        say("read both"),
    ]
    a, client = make_agent(replies)
    a.run("read both files")
    tools = [m for m in a.messages if m["role"] == "tool"]
    assert len(tools) == 2
    assert all(m["tool_name"] == "read_file" for m in tools)
    assert "alpha" in tools[0]["content"] and "beta" in tools[1]["content"]


def test_assistant_tool_calls_are_recorded_for_the_model(make_agent, tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n")
    a, _ = make_agent([Reply(content="", tool_calls=[call("read_file", path="a.txt")]),
                       say("ok")])
    a.run("read it")
    asst = next(m for m in a.messages if m.get("tool_calls"))
    assert asst["tool_calls"][0]["function"]["name"] == "read_file"


# --------------------------------------------------------------------- #
# failure isolation


def test_unknown_tool_name_lists_what_exists(make_agent):
    a, _ = make_agent([Reply(content="", tool_calls=[call("nonesuch")]), say("ok")])
    a.run("go")
    result = next(m for m in a.messages if m["role"] == "tool")
    assert "No tool named 'nonesuch'" in result["content"]
    assert "read_file" in result["content"]
    assert result["tool_name"] == "nonesuch"


def test_a_crashing_tool_does_not_kill_the_session(make_agent, monkeypatch):
    a, _ = make_agent([Reply(content="", tool_calls=[call("read_file", path="x")]),
                       say("recovered")])

    def boom(args, ctx):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(a.registry["read_file"], "run", boom)

    final = a.run("go")
    assert final == "recovered"
    result = next(m for m in a.messages if m["role"] == "tool")
    assert "read_file raised RuntimeError: disk on fire" in result["content"]


def test_missing_argument_returns_the_schema(make_agent, monkeypatch):
    a, _ = make_agent([Reply(content="", tool_calls=[call("read_file")]), say("ok")])

    def needs_path(args, ctx):
        raise KeyError("path")
    monkeypatch.setattr(a.registry["read_file"], "run", needs_path)

    a.run("go")
    result = next(m for m in a.messages if m["role"] == "tool")
    assert "Missing required argument" in result["content"]
    assert "properties" in result["content"]


# --------------------------------------------------------------------- #
# loop control


def test_plain_reply_ends_the_loop(make_agent):
    a, client = make_agent([say("nothing to do")])
    assert a.run("hi") == "nothing to do"
    assert len(client.seen) == 1


def test_turn_limit_reports_rather_than_hanging(make_agent, tmp_path):
    (tmp_path / "a.txt").write_text("x\n")
    endless = [Reply(content="", tool_calls=[call("read_file", path="a.txt")])
               for _ in range(10)]
    a, _ = make_agent(endless, max_turns=3)
    final = a.run("loop forever")
    assert "Stopped after 3 tool rounds" in final


def test_turn_limit_asks_for_a_summary_without_touching_the_transcript(make_agent, tmp_path):
    """The limit message is a real model answer (what was done / left / next),
    not the bare notice -- and the extra no-tools call must not land in the
    transcript, since a later /continue has to see the same context it would
    have seen had the limit not been hit.
    """
    (tmp_path / "a.txt").write_text("x\n")
    endless = [Reply(content="", tool_calls=[call("read_file", path="a.txt")])
               for _ in range(3)]
    summary = say("Did: read a.txt twice. Left: nothing verified. Next: run the tests.")
    a, client = make_agent(endless + [summary], max_turns=3)

    before = len(a.messages)
    final = a.run("loop forever")

    assert "Did: read a.txt twice" in final
    assert "/continue" in final
    # 3 tool-round calls plus the one extra summary call -- and the summary
    # call is never appended to the transcript.
    assert len(client.seen) == 4
    assert client.seen[-1][-1]["content"].startswith("You have used up the tool-round budget")
    # briefing + user turn + 3 x (assistant + tool result); no summary turn added.
    assert len(a.messages) == before + 8
    assert a.messages[-1]["role"] == "tool"


def test_limit_summary_falls_back_when_the_model_call_fails(make_agent, tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x\n")
    endless = [Reply(content="", tool_calls=[call("read_file", path="a.txt")])
               for _ in range(2)]
    a, client = make_agent(endless, max_turns=2)

    original_chat = client.chat

    def boom(*a_, **kw):
        # Let the two scripted turns through; only the extra no-tools
        # summary call (after replies run out) fails.
        if client.replies:
            return original_chat(*a_, **kw)
        raise RuntimeError("model unreachable")
    monkeypatch.setattr(client, "chat", boom)

    final = a.run("loop forever")
    assert "Stopped after 2 tool rounds" in final
    assert "/continue" in final


def test_continue_after_the_limit_resumes_with_a_fresh_budget(make_agent, tmp_path):
    (tmp_path / "a.txt").write_text("x\n")
    endless = [Reply(content="", tool_calls=[call("read_file", path="a.txt")])
               for _ in range(2)]
    summary = say("Did: partial read. Left: verify. Next: finish the read.")
    a, client = make_agent(endless + [summary], max_turns=2)

    limit_reply = a.run("start the task")
    assert "/continue" in limit_reply

    # A fresh call to run() -- what typing "continue" or /continue produces --
    # gets its own full range(max_turns) budget, same as any other message.
    client.replies.append(say("all done"))
    result = a.run("continue")
    assert result == "all done"


def test_events_are_emitted_for_each_phase(make_agent, tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x\n")
    seen = []
    client = FakeClient([Reply(content="", tool_calls=[call("read_file", path="a.txt")]),
                         say("ok")])
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    a = Agent(Config(), str(tmp_path), approve=lambda *_: Approval.GRANTED,
              on_event=lambda kind, data: seen.append(kind))
    a.run("go")
    assert seen.count("assistant") == 2
    assert "tool_start" in seen and "tool_end" in seen


# ------------------------------------------------------- harness-held state --

def test_objective_is_captured_verbatim_and_pinned_every_turn(make_agent):
    """The model must see the user's literal words on turn 20, not a summary."""
    a, client = make_agent([Reply(content="", tool_calls=[call("read_file", path="a.txt")]),
                            say("ok")])
    a.run("RENAME the widget to gadget, exactly")

    assert a.state.objective == "RENAME the widget to gadget, exactly"
    for sent in client.seen:
        pinned = [m for m in sent if m["role"] == "system"
                  and "[wilbur:briefing]" in (m.get("content") or "")]
        assert len(pinned) == 1, "briefing missing or duplicated on a turn"
        assert "RENAME the widget to gadget, exactly" in pinned[0]["content"]
        assert sent.index(pinned[0]) == 1, "briefing must sit directly after the system prompt"


def test_briefing_is_replaced_not_appended(make_agent):
    """A stale copy further down would contradict the live one and the model
    has no way to tell which is current."""
    a, client = make_agent([Reply(content="", tool_calls=[call("read_file", path="a.txt")]),
                            Reply(content="", tool_calls=[call("read_file", path="a.txt")]),
                            say("ok")])
    a.run("do the thing")
    briefings = [m for m in a.messages if m["role"] == "system"
                 and "[wilbur:briefing]" in (m.get("content") or "")]
    assert len(briefings) == 1


def test_a_failed_tool_call_enters_the_ledger(make_agent):
    a, _ = make_agent([Reply(content="", tool_calls=[call("read_file", path="missing.txt")]),
                       say("ok")])
    a.run("read it")
    assert a.state.failures, "a failing tool call was not recorded"


def test_repeating_a_failed_call_is_called_out_in_the_result(make_agent):
    """Local models reissue the identical failing call. The harness says so
    rather than letting it happen a third time silently."""
    a, _ = make_agent([
        Reply(content="", tool_calls=[call("read_file", path="missing.txt")]),
        Reply(content="", tool_calls=[call("read_file", path="missing.txt")]),
        say("ok"),
    ])
    a.run("read it")
    tool_msgs = [m for m in a.messages if m.get("role") == "tool"]
    assert any("attempt 2" in (m.get("content") or "") for m in tool_msgs), \
        "the second identical failure was not flagged"


def test_verification_runs_after_a_write_and_lands_in_the_result(make_agent, tmp_path, monkeypatch):
    """The whole point of forced verification: the model does not have to
    remember, and cannot claim a change works when the check says otherwise."""
    import wilbur.verify as verify_mod
    monkeypatch.setattr(verify_mod, "detect",
                        lambda cwd: verify_mod.Check("fake", ["sh", "-c", "echo nope >&2; exit 1"]))
    a, _ = make_agent([Reply(content="", tool_calls=[
        call("write_file", path="new.txt", content="hello")]), say("ok")])
    a.run("write a file")

    tool_msgs = [m for m in a.messages if m.get("role") == "tool"]
    assert any("VERIFICATION FAILED" in (m.get("content") or "") for m in tool_msgs)
    assert a.state.verifications, "the verdict was not recorded in state"


def test_no_verification_when_the_project_defines_no_check(make_agent, monkeypatch):
    """Never invent a command to run against someone's repository."""
    import wilbur.verify as verify_mod
    monkeypatch.setattr(verify_mod, "detect", lambda cwd: None)
    a, _ = make_agent([Reply(content="", tool_calls=[
        call("write_file", path="new.txt", content="hello")]), say("ok")])
    a.run("write a file")
    tool_msgs = [m for m in a.messages if m.get("role") == "tool"]
    assert not any("VERIFICATION" in (m.get("content") or "") for m in tool_msgs)
    assert not a.state.verifications


def test_read_only_tools_do_not_trigger_verification(make_agent, monkeypatch):
    import wilbur.verify as verify_mod
    called = []
    monkeypatch.setattr(verify_mod, "detect", lambda cwd: called.append(1))
    a, _ = make_agent([Reply(content="", tool_calls=[call("read_file", path="a.txt")]),
                       say("ok")])
    a.run("read it")
    assert not called, "a read triggered the project's test suite"


# --------------------------------------------------------------------- #
# cooperative cancellation (Ctrl-C reaching a subagent on its own thread)


def test_cancel_before_a_round_stops_the_loop_without_calling_the_model(make_agent):
    a, client = make_agent([say("should not be reached")])
    a.cancel_event.set()
    result = a.run("do something")
    assert result == "Cancelled by user."
    assert client.seen == [], "the model must not be called once cancelled"


def test_cancel_between_tool_calls_stops_the_remaining_calls_in_that_round(make_agent, tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "b.txt").write_text("beta\n")
    replies = [
        Reply(content="", tool_calls=[call("read_file", path="a.txt"),
                                      call("read_file", path="b.txt")]),
        say("done"),
    ]
    a, client = make_agent(replies)

    real_execute = a._execute
    def cancel_after_first(c):
        result = real_execute(c)
        a.cancel_event.set()
        return result
    a._execute = cancel_after_first

    result = a.run("read both files")
    assert result == "Cancelled by user."
    tools = [m for m in a.messages if m["role"] == "tool"]
    assert len(tools) == 1, "the second call must not have run after cancellation"


def test_not_cancelled_runs_normally(make_agent):
    a, client = make_agent([say("all good")])
    result = a.run("hello")
    assert result == "all good"
