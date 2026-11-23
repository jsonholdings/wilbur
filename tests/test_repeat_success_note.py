"""Repeat-success loop (2.44.2).

The failure ledger only counted FAILED repeats, so a model reissuing the
identical successful call with the identical output went unflagged (session
desktopuser1-1792984315: the same run_bash 4 times, same output). The state
now keeps a success-repeat ledger keyed on call signature AND output -- a
call that returns something new is progress, not a repeat -- and on the 3rd
identical one the tool result carries a [harness] note.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import wilbur.agent as agent_mod
from wilbur.agent import Agent
from wilbur.config import Config
from wilbur.fake_backend import FakeClient
from wilbur.llm import Reply, ToolCall
from wilbur.state import RunState
from wilbur.tools.base import Approval, ToolResult


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


def _stub_tool(a, output):
    """Point read_file at a fixed successful result."""
    a.registry["read_file"].run = lambda args, ctx: ToolResult(output=output)


# --------------------------------------------------------------------- #
# state ledger


def test_ledger_counts_identical_signature_and_output():
    st = RunState()
    sig = "read_file(path=a.txt)"
    st.record_success(sig, "Attempt 1/5...")
    assert st.repeated_success(sig, "Attempt 1/5...") == 1
    st.record_success(sig, "Attempt 1/5...")
    assert st.repeated_success(sig, "Attempt 1/5...") == 2
    st.record_success(sig, "Attempt 1/5...")
    assert st.repeated_success(sig, "Attempt 1/5...") == 3


def test_ledger_does_not_count_a_different_output():
    st = RunState()
    sig = "read_file(path=a.txt)"
    st.record_success(sig, "Attempt 1/5...")
    st.record_success(sig, "Attempt 1/5...")
    st.record_success(sig, "Attempt 2/5...")
    assert st.repeated_success(sig, "Attempt 2/5...") == 1


def test_ledger_does_not_count_a_different_call():
    st = RunState()
    st.record_success("read_file(path=a.txt)", "x")
    st.record_success("read_file(path=a.txt)", "x")
    st.record_success("read_file(path=b.txt)", "x")
    assert st.repeated_success("read_file(path=b.txt)", "x") == 1


# --------------------------------------------------------------------- #
# agent: the [harness] note


def test_third_identical_successful_call_gets_a_harness_note(make_agent):
    a, _ = make_agent([say("ok")])
    _stub_tool(a, "Attempt 1/5...")
    c = call("read_file", path="a.txt")
    assert "[harness]" not in a._execute(c)
    assert "[harness]" not in a._execute(c), "the 2nd identical call must not trigger it"
    third = a._execute(c)
    assert "[harness]" in third, "the 3rd identical call must be flagged"
    assert "3rd time" in third


def test_two_identical_calls_do_not_trigger(make_agent):
    a, _ = make_agent([say("ok")])
    _stub_tool(a, "Attempt 1/5...")
    c = call("read_file", path="a.txt")
    assert "[harness]" not in a._execute(c)
    assert "[harness]" not in a._execute(c)


def test_third_call_with_different_output_does_not_trigger(make_agent):
    a, _ = make_agent([say("ok")])
    c = call("read_file", path="a.txt")
    _stub_tool(a, "Attempt 1/5...")
    a._execute(c)
    _stub_tool(a, "Attempt 2/5...")
    a._execute(c)
    _stub_tool(a, "Attempt 3/5...")
    third = a._execute(c)
    assert "[harness]" not in third, "a new output is progress, not a repeat"
