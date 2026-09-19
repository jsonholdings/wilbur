"""A hang or crash mid-turn must still leave a resumable session log.

Root cause fixed here: session.save() used to run once, after Agent.run()
returned -- so a turn that hung or raised left nothing on disk at all
(established defect, 2026-09-18). Agent now emits a "message" event after
every transcript append (assistant reply, each tool result), and Repl saves
on that event, not only at the end of the exchange.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import wilbur.agent as agent_mod
from wilbur.agent import Agent
from wilbur.config import Config
from wilbur.llm import Reply, ToolCall
from wilbur.repl import Repl
from wilbur.tools.base import Approval


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)

    def __call__(self, *a, **kw):
        return self

    def chat(self, messages, schema, **kw):
        return self.replies.pop(0) if self.replies else Reply(content="done", tool_calls=[])


def call(name, **args):
    return ToolCall(name=name, arguments=args)


def say(text):
    return Reply(content=text, tool_calls=[])


@pytest.fixture
def make_agent(monkeypatch, tmp_path):
    def build(replies):
        client = FakeClient(replies)
        monkeypatch.setattr(agent_mod, "OllamaClient", client)
        config = Config()
        a = Agent(config, str(tmp_path), approve=lambda *_: Approval.GRANTED)
        return a
    return build


def test_message_event_fires_once_per_transcript_append(make_agent, tmp_path):
    (tmp_path / "a.txt").write_text("x\n")
    seen = []
    a = make_agent([Reply(content="", tool_calls=[call("read_file", path="a.txt")]),
                    say("ok")])
    a.on_event = lambda kind, data: seen.append(kind) if kind == "message" else None
    a.run("go")
    # one assistant message with the tool call, one tool result, one final
    # assistant message with no tool calls: three appends, three events.
    assert seen.count("message") == 3


def _bare_repl(tmp_path, monkeypatch):
    """A Repl with no network/model-manager setup, wired only for _on_event."""
    repl = object.__new__(Repl)
    repl.session_id = "test-session"
    repl.cwd = str(tmp_path)
    from wilbur.state import RunState
    repl.state = RunState()
    repl.config = Config()
    repl.stats = {"calls": 0, "in_tokens": 0, "out_tokens": 0, "seconds": 0.0}
    saves = []
    import wilbur.session as sessions_mod
    monkeypatch.setattr(sessions_mod, "save", lambda *a, **kw: saves.append(a))
    repl.sessions = sessions_mod
    client = FakeClient([Reply(content="", tool_calls=[call("read_file", path="a.txt")]),
                        say("ok")])
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    repl.agent = Agent(repl.config, str(tmp_path), approve=lambda *_: Approval.GRANTED,
                       on_event=repl._on_event, state=repl.state)
    return repl, saves


def test_repl_saves_the_session_after_every_message_not_only_at_turn_end(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x\n")
    repl, saves = _bare_repl(tmp_path, monkeypatch)
    repl.agent.run("go")
    # three transcript appends this turn (assistant+tool_calls, tool result,
    # final assistant) -> three incremental saves, not one save at the end.
    assert len(saves) == 3


def test_ctrl_c_mid_tool_call_still_leaves_prior_messages_saved(tmp_path, monkeypatch):
    """A Ctrl-C during a running tool must propagate out of Agent.run() (Repl
    catches it and continues the REPL loop) but must not erase the incremental
    saves already made for messages appended earlier in the same turn."""
    (tmp_path / "a.txt").write_text("x\n")
    repl, saves = _bare_repl(tmp_path, monkeypatch)

    def interrupted(args, ctx):
        raise KeyboardInterrupt()
    monkeypatch.setattr(repl.agent.registry["read_file"], "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        repl.agent.run("go")
    # the assistant message proposing the tool call was appended and saved
    # before the interrupt hit; that save survives even though the turn
    # never completed.
    assert len(saves) == 1


def test_a_crash_mid_turn_still_leaves_the_partial_transcript_saved(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x\n")
    repl, saves = _bare_repl(tmp_path, monkeypatch)

    def boom(args, ctx):
        raise RuntimeError("simulated hang/crash")
    monkeypatch.setattr(repl.agent.registry["read_file"], "run", boom)
    repl.agent.run("go")
    # the crashing tool call still produced a tool message and a final reply,
    # each triggering an incremental save -- nothing is lost to the crash.
    assert len(saves) >= 2
