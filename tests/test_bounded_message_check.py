"""bounded-message-check (BACKLOG, owner HIGH 2026-10-16): a message typed
while Wilbur works is read within `message_check_max_s`, not only when the
whole turn ends. FakeClient only; no model, no GPU."""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur import inbox as inbox_mod
from wilbur import runstate, status
from wilbur.config import Config
from wilbur.inbox import Inbox
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.shell import Bash as BashTool

from conftest import call, say

MSG = "stop and fix the README first"


def _flat(messages):
    return "\n".join(str(m.get("content", "")) for m in messages)


def _agent_with_message_during_first_tool(make_agent, wire_inbox, line=MSG):
    q: "queue.Queue[str | None]" = queue.Queue()
    sent = []

    def approve(*_):
        if not sent:          # the user types while the first tool call runs
            q.put(line)
            sent.append(line)
        return Approval.GRANTED

    agent, client = make_agent(
        [say_tool("echo one"), say_tool("echo two"), say("done")], approve=approve)
    if wire_inbox:
        box = Inbox(q)
        agent.inbox = box
        agent.ctx.inbox = box
    return agent, client, q


def say_tool(cmd):
    from wilbur.llm import Reply
    return Reply(content="", tool_calls=[call("run_bash", command=cmd)])


def test_message_typed_during_a_tool_call_is_read_at_the_next_round(make_agent):
    agent, client, q = _agent_with_message_during_first_tool(make_agent, wire_inbox=True)
    agent.run("do the work")
    # the very next model call (round 2) already carries the message
    assert MSG in _flat(client.seen[1])
    assert MSG not in _flat(client.seen[0])
    assert q.empty()


def test_negative_control_without_the_inbox_the_message_waits_for_turn_end(make_agent):
    agent, client, q = _agent_with_message_during_first_tool(make_agent, wire_inbox=False)
    agent.run("do the work")
    assert all(MSG not in _flat(seen) for seen in client.seen)
    assert q.get_nowait() == MSG


def test_slash_command_ends_the_turn_for_the_repl(make_agent):
    agent, client, q = _agent_with_message_during_first_tool(
        make_agent, wire_inbox=True, line="/queue")
    final = agent.run("do the work")
    assert final.startswith("Paused")
    assert len(client.seen) == 1          # no second model call
    assert q.get_nowait() == "/queue"


def _ctx(tmp_path, box, bound):
    cfg = Config()
    cfg.message_check_max_s = bound
    return ToolContext(cwd=str(tmp_path), config=cfg, read_files={},
                       approve=lambda *_: Approval.GRANTED, inbox=box)


def test_pending_message_preempts_a_sleep(tmp_path):
    q: "queue.Queue[str | None]" = queue.Queue()
    box = Inbox(q)
    timer = threading.Timer(0.3, lambda: q.put(MSG))
    timer.start()
    started = time.monotonic()
    try:
        result = BashTool().run({"command": "sleep 30", "timeout": 60},
                                _ctx(tmp_path, box, bound=0.4))
    finally:
        timer.cancel()
        timer.join()
    assert time.monotonic() - started < 5
    assert "new message" in result.output


def test_sleep_without_a_message_runs_to_completion(tmp_path):
    box = Inbox(queue.Queue())
    result = BashTool().run({"command": "sleep 0.5", "timeout": 10},
                            _ctx(tmp_path, box, bound=0.4))
    assert not result.is_error and "interrupted" not in result.output


def test_only_wait_commands_are_preempted():
    assert inbox_mod.is_wait_command("sleep 600")
    assert inbox_mod.is_wait_command("timeout 30 sleep 5")
    assert inbox_mod.is_wait_command("until [ -f done ]; do sleep 5; done")
    assert not inbox_mod.is_wait_command("make test")
    assert not inbox_mod.is_wait_command("pytest -q && sleep 1 && echo ok")


def test_unread_clock_and_mark_read():
    now = [1000.0]
    q: "queue.Queue[str | None]" = queue.Queue()
    box = Inbox(q, clock=lambda: now[0])
    assert box.unread_since() is None
    q.put(MSG)
    box.note_arrival()
    assert box.unread_since() == 1000.0
    q.get_nowait()                         # the idle REPL reads it
    box.mark_read()
    assert box.unread_since() is None


def test_idle_repl_reads_a_message_at_once(monkeypatch, tmp_path):
    from test_runstate import _repl
    repl = _repl(monkeypatch, tmp_path, [say("hi")])
    repl._input_q.put(MSG)
    started = time.monotonic()
    assert repl._next_line_within(5) == MSG
    assert time.monotonic() - started < 1
    assert repl.inbox.unread_since() is None


def test_status_check_fails_on_a_stale_unread_message(capsys):
    bound = Config().message_check_max_s
    runstate.write("msg", state=runstate.WORKING,
                   unread_message_since=time.time() - 3 * bound)
    assert status.status_main(["--check"]) == 1
    assert "unread for" in capsys.readouterr().err
    # inside 2x the bound is fine (the agent is about to read it)
    runstate.write("msg", state=runstate.WORKING,
                   unread_message_since=time.time() - bound)
    assert status.status_main(["--check"]) == 0
    # control: nothing unread
    runstate.write("msg", state=runstate.WORKING)
    assert status.status_main(["--check"]) == 0


def test_plain_line_then_slash_command_ends_the_turn_at_once(make_agent):
    q: "queue.Queue[str | None]" = queue.Queue()
    sent = []

    def approve(*_):
        if not sent:
            q.put(MSG)
            q.put("/queue")
            sent.append(1)
        return Approval.GRANTED

    agent, client = make_agent(
        [say_tool("echo one"), say_tool("echo two"), say("done")], approve=approve)
    box = Inbox(q)
    agent.inbox = box
    agent.ctx.inbox = box
    final = agent.run("do the work")
    assert len(client.seen) == 1            # no extra model call before /queue
    assert MSG in _flat(agent.messages)     # the line is kept for the next turn
    assert final.startswith("Paused")       # /queue handed to the REPL
    assert q.get_nowait() == "/queue"


def test_loop_hands_back_between_turns_when_a_command_waits(make_agent):
    from wilbur import loop
    agent, _ = make_agent([say("working"), say("still working"), say("more")])
    waiting = []
    result = loop.run_goal_loop(agent, "goal", max_turns=5,
                                interrupt=lambda: bool(waiting),
                                on_turn=lambda turn, reply: waiting.append(turn))
    assert result.status == "user_message" and result.turns == 1


def test_loop_control_without_interrupt_keeps_going(make_agent):
    from wilbur import loop
    agent, _ = make_agent([say("working"), say("still working"), say("more")])
    result = loop.run_goal_loop(agent, "goal", max_turns=3)
    assert result.status != "user_message"


def test_watcher_records_and_clears_unread_in_run_json(monkeypatch, tmp_path):
    from test_runstate import _repl
    repl = _repl(monkeypatch, tmp_path, [say("hi")])
    repl._set_runstate(runstate.WORKING)
    repl._turn_active.set()
    repl._deliver_line(MSG)
    repl._sync_unread()
    assert runstate.read(repl.session_id)["unread_message_since"]
    repl.inbox.take_injectable()            # the agent read it
    repl._sync_unread()
    assert runstate.read(repl.session_id)["unread_message_since"] is None
    repl._turn_active.clear()
    runstate.remove(repl.session_id)


def test_eof_alone_does_not_pause_the_turn(make_agent):
    q: "queue.Queue[str | None]" = queue.Queue()
    q.put(None)                              # stdin closed (piped input)
    agent, client = make_agent([say_tool("echo one"), say("done")])
    box = Inbox(q)
    agent.inbox = box
    agent.ctx.inbox = box
    assert not box.pending()
    final = agent.run("do the work")
    assert not final.startswith("Paused")
    assert len(client.seen) == 2
    q.put("/queue")                          # control: a real command is pending
    assert box.pending()


def test_a_line_the_model_worked_past_does_not_stop_keep_going(make_agent):
    # 2.43.1: session desktopuser1-1793148787 (2026-10-27) took a line at
    # message 15, worked ~120 more messages, hit the round cap, and keep-going
    # stopped with user_interrupt on that long-answered line.
    agent, client, q = _agent_with_message_during_first_tool(make_agent, wire_inbox=True)
    agent.run("do the work")
    assert MSG in _flat(client.seen[1])
    assert agent.turn_messages_injected == 0


def test_a_line_answered_in_the_final_reply_still_wins_over_the_nudge(make_agent):
    q: "queue.Queue[str | None]" = queue.Queue()

    def approve(*_):
        q.put(MSG)
        return Approval.GRANTED

    agent, client = make_agent([say_tool("echo one"), say("ok, README first")], approve=approve)
    box = Inbox(q)
    agent.inbox = box
    agent.ctx.inbox = box
    agent.run("do the work")
    assert MSG in _flat(client.seen[1])
    assert agent.turn_messages_injected == 1
