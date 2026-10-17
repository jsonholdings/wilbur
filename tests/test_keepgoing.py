"""Keep going: a plain multi-item prompt runs to the end without "continue".

Owner, 2026-10-02: "I had to keep telling him to continue between work items".
`Agent.run` ends a turn on any reply without a tool call; `Repl._keep_going`
nudges it on while the model's plan is open or it announced a next step, and
stops on a question, all-done, a refused approval, Ctrl-C, a typed line, a
stall, or the cap. Driven through the real `Repl.run` against FakeClient.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.agent as agent_mod
from wilbur import keepgoing
from wilbur.config import Config
from wilbur.llm import Reply
from wilbur.repl import Repl
from wilbur.state import RunState

from conftest import FakeClient, say, call

PROMPT = "1. create a.txt 2. create b.txt 3. create c.txt"
PLAN = [{"task": f"create {n}.txt", "status": "pending"} for n in "abc"]


def _plan(done: int) -> Reply:
    todos = [dict(t, status="completed" if i < done else "pending") for i, t in enumerate(PLAN)]
    return Reply(content="", tool_calls=[call("todo_write", todos=todos)])


def _write(name: str) -> Reply:
    return Reply(content="", tool_calls=[call("write_file", path=name, content=name)])


def _run(monkeypatch, tmp_path, replies, lines=(PROMPT,), approve_mode="auto-edit",
         configure=None):
    client = FakeClient(replies)
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    r_fd, w_fd = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(r_fd))
    os.close(w_fd)  # stdin closes once the queued lines are used up
    cfg = Config()
    if configure:
        configure(cfg)
    repl = Repl(cfg, str(tmp_path))
    repl.manager.available = lambda: True
    repl._use_ptk = False
    repl.approve_mode = approve_mode
    for line in lines:
        repl._input_q.put(line)
    t = threading.Thread(target=repl.run, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive()
    return repl, client


def _nudges(client) -> int:
    last = client.seen[-1] if client.seen else []
    return sum(1 for m in last if m.get("role") == "user" and m["content"] == keepgoing.NUDGE)


def test_open_plan_runs_all_three_items_with_no_user_input(monkeypatch, tmp_path, capsys):
    replies = [_plan(0), _write("a.txt"), _plan(1), say("Item 1 done. Next, I'll create b.txt."),
               _write("b.txt"), _plan(2), say("b.txt created."),
               _write("c.txt"), _plan(3), say("All three items are done.")]
    repl, client = _run(monkeypatch, tmp_path, replies)
    assert all((tmp_path / f"{n}.txt").exists() for n in "abc")
    out = capsys.readouterr().out
    assert "↻ continuing (1)" in out and "↻ continuing (2)" in out
    assert "continuing (3)" not in out
    assert _nudges(client) == 2


def test_announced_next_step_without_a_plan_continues(monkeypatch, tmp_path, capsys):
    replies = [_write("a.txt"), say("Created a.txt. Next, I'll create b.txt."),
               _write("b.txt"), say("Done: b.txt is written. That's everything.")]
    _run(monkeypatch, tmp_path, replies)
    assert (tmp_path / "b.txt").exists()
    assert "continuing (1)" in capsys.readouterr().out


def test_question_to_the_user_stops(monkeypatch, tmp_path, capsys):
    replies = [_plan(0), _write("a.txt"), _plan(1),
               say("a.txt is done. Should b.txt be empty or contain text?"),
               _write("b.txt")]
    repl, client = _run(monkeypatch, tmp_path, replies)
    assert not (tmp_path / "b.txt").exists()
    assert "continuing" not in capsys.readouterr().out


def test_all_done_does_not_continue(monkeypatch, tmp_path, capsys):
    replies = [_plan(0), _plan(3), say("All three items are done."), _write("x.txt")]
    _run(monkeypatch, tmp_path, replies)
    assert not (tmp_path / "x.txt").exists()
    assert "continuing" not in capsys.readouterr().out


def test_cap_stops_and_says_so(monkeypatch, tmp_path, capsys):
    replies = [_plan(0)] + [r for _ in range(5)
                            for r in (_write("a.txt"), say("Next, I'll keep going."))]

    def cap2(cfg):
        cfg.auto_continue_max = 2
    _run(monkeypatch, tmp_path, replies, configure=cap2)
    out = capsys.readouterr().out
    assert "continuing (2/2)" in out and "continuing (3/" not in out
    assert "cap reached (2/2)" in out


def test_wall_clock_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "OllamaClient", FakeClient([]))
    repl = Repl(Config(), str(tmp_path))
    repl.config.auto_continue_max_minutes = 1
    repl.state.set_plan(PLAN)
    times = iter([0.0, 61.0, 61.0])
    ran = []
    repl.agent.run = lambda text: ran.append(text) or "Next, I'll go on."
    out = repl._keep_going("Next, I'll go on.", clock=lambda: next(times))
    assert ran == [] and out == "Next, I'll go on."


def test_refused_approval_stops(monkeypatch, tmp_path, capsys):
    replies = [_plan(0), Reply(content="", tool_calls=[call("run_bash", command="touch a.txt")]),
               say("I couldn't run that. Next, I'll try again."), _write("b.txt")]
    # approve_mode "ask" with no interactive input -> UNAVAILABLE -> refused.
    monkeypatch.setattr(sys.stdin.__class__, "isatty", lambda self: False, raising=False)
    repl, client = _run(monkeypatch, tmp_path, replies, approve_mode="ask")
    assert not (tmp_path / "b.txt").exists()
    assert "continuing" not in capsys.readouterr().out


def test_ctrl_c_during_a_continue_stops(monkeypatch, tmp_path, capsys):
    class _Interrupting(FakeClient):
        def chat(self, messages, schema, **kw):
            if any(m.get("content") == keepgoing.NUDGE for m in messages):
                raise KeyboardInterrupt
            return super().chat(messages, schema, **kw)
    client = _Interrupting([_plan(0), say("Next, I'll create b.txt."), _write("b.txt")])
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    monkeypatch.setattr(Repl, "_detect_ptk", staticmethod(lambda: False))
    r_fd, w_fd = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(r_fd))
    os.close(w_fd)
    repl = Repl(Config(), str(tmp_path))
    repl.manager.available = lambda: True
    repl._input_q.put(PROMPT)
    repl.run()
    out = capsys.readouterr().out
    assert "interrupted" in out and not (tmp_path / "b.txt").exists()


def test_off_in_config_and_by_command(monkeypatch, tmp_path, capsys):
    replies = [_plan(0), say("Next, I'll create a.txt."), _write("a.txt")]
    _run(monkeypatch, tmp_path, list(replies), lines=("/keepgoing off", PROMPT))
    assert not (tmp_path / "a.txt").exists()

    def off(cfg):
        cfg.auto_continue = False
    _run(monkeypatch, tmp_path, list(replies), configure=off)
    out = capsys.readouterr().out
    assert "keep going: off" in out and "continuing" not in out


def test_a_typed_line_wins_over_the_nudge(monkeypatch, tmp_path, capsys):
    replies = [_plan(0), say("Next, I'll create a.txt."), say("ok, stopped.")]
    repl, client = _run(monkeypatch, tmp_path, replies, lines=(PROMPT, "stop there"))
    assert "continuing" not in capsys.readouterr().out
    # 2.35.0: the line is read mid-turn (wilbur.inbox, with a "new message"
    # header) and still stops keep-going instead of a nudge.
    assert any("stop there" in (m.get("content") or "") for m in client.seen[-1])
    assert repl._stop_reason == "user_interrupt"


def test_repeated_no_progress_trips_the_stuck_detector(monkeypatch, tmp_path, capsys):
    # A run that announces a next step every turn but never changes anything is
    # stopped by the stuck detector (wilbur.horizon) after stuck_limit
    # no-progress turns -- the numeric continue cap no longer does this.
    replies = [_plan(0)] + [say("Next, I'll create a.txt.") for _ in range(8)]

    def small_stuck(cfg):
        cfg.stuck_limit = 3

    _run(monkeypatch, tmp_path, replies, configure=small_stuck)
    out = capsys.readouterr().out
    assert "no further progress" in out
    assert "continuing (4)" in out and "continuing (5)" not in out


@pytest.mark.parametrize("reply,plan_done,go", [
    ("Next, I'll create add.py.", None, True),
    ("Now let me write the notes.", None, True),
    ("Created hello.txt.", 1, True),            # plan 1/3 open
    ("All three items are done.", 3, False),
    ("Done. Would you like me to add tests?", 1, False),
    ("Which option do you prefer?", None, False),
    ("Created hello.txt.", None, False),
    ("", None, False),
    ("OK, stopped there as you asked.", 1, False),
    ("Cancelled by user.", 0, False),
    # Asking leave to carry on with the request itself continues (2.23.1).
    ("Created a.txt. Shall I continue with the next item?", None, True),
    ("Item 1 is in. Want me to proceed?", 1, True),
    ("b.txt written. Should I move on to step 3?", None, True),
    # A real choice, new scope, or a done list still stops.
    ("Should I continue with SQLite or switch to Postgres?", None, False),
    ("Which file should I proceed with?", None, False),
    ("All three items are done. Shall I continue?", 3, False),
    ("Shall I proceed with the deploy to production?", None, False),
])
def test_decide(reply, plan_done, go):
    state = RunState()
    if plan_done is not None:
        state.set_plan([dict(t, status="completed" if i < plan_done else "pending")
                        for i, t in enumerate(PLAN)])
    assert keepgoing.decide(reply, state).go is go


@pytest.mark.parametrize("worked,plan_done,go", [
    (True, None, True),    # went quiet after tool work: the commonest real stop
    (True, 1, True),
    (False, None, False),  # said nothing and did nothing: not a task in flight
    (False, 1, False),
])
def test_decide_empty_reply(worked, plan_done, go):
    state = RunState()
    if plan_done is not None:
        state.set_plan([dict(t, status="completed" if i < plan_done else "pending")
                        for i, t in enumerate(PLAN)])
    assert keepgoing.decide("", state, worked=worked).go is go


def test_quiet_after_tool_work_continues(monkeypatch, tmp_path, capsys):
    # Measured on the owner's saved sessions (2026-10-02): the model calls tools,
    # then ends the turn with an empty reply, mid-task. 2.23.0 stopped there.
    replies = [_write("a.txt"), say(""), _write("b.txt"), say("Both files are written.")]
    _run(monkeypatch, tmp_path, replies, lines=("create a.txt and b.txt",))
    # The empty mid-task reply is recovered inside the turn (stall-(b) fix,
    # agent.run) so the work still finishes; b.txt proves it did not stop dead.
    assert (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()
    assert "Both files are written." in capsys.readouterr().out


def test_two_empty_replies_stop_with_empty_reply(monkeypatch, tmp_path, capsys):
    # The agent's one empty-reply retry also came back empty: keep-going stops
    # and names the cause (stopped-state-is-visible spec item 3, 2026-10-16),
    # instead of nudging on and later stopping under an unrelated reason.
    replies = [_write("a.txt"), say(""), say(""), _write("b.txt")]
    repl, _ = _run(monkeypatch, tmp_path, replies, lines=("create a.txt and b.txt",))
    out = capsys.readouterr().out
    assert "continuing" not in out and not (tmp_path / "b.txt").exists()
    assert "two empty replies" in out and "STOPPED \u2014 empty_reply" in out
    assert repl._stop_reason == "empty_reply"


def test_asking_leave_to_carry_on_continues(monkeypatch, tmp_path, capsys):
    replies = [_write("a.txt"), say("a.txt is done. Shall I continue with the next item?"),
               _write("b.txt"), say("Both files are written.")]
    _run(monkeypatch, tmp_path, replies, lines=("create a.txt and b.txt",))
    out = capsys.readouterr().out
    assert "asked leave to carry on" in out and (tmp_path / "b.txt").exists()


def test_a_real_choice_still_stops(monkeypatch, tmp_path, capsys):
    replies = [_write("a.txt"), say("Should b.txt be JSON or YAML?"), _write("b.txt")]
    _run(monkeypatch, tmp_path, replies, lines=("create a.txt and b.txt",))
    out = capsys.readouterr().out
    assert "continuing" not in out and not (tmp_path / "b.txt").exists()
