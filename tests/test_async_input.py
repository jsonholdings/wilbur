"""Async input: the owner can type and queue commands while a turn runs.

BACKLOG `async-input-prompt-while-agent-runs`. `Repl` used to call the
blocking `input()` builtin directly in both its main loop and its approval
prompt, so nothing the owner typed was even read until the current turn (or
approval) returned control to that one blocking call. These tests drive the
real pump thread (`Repl._pump_stdin`) over a real pipe standing in for
stdin, against the deterministic `FakeClient` fake backend -- no Ollama, no
model load.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.agent as agent_mod
from wilbur.config import Config
from wilbur.llm import Reply
from wilbur.repl import Repl

from conftest import FakeClient, say


class _GatedClient(FakeClient):
    """Like FakeClient, but its first `chat()` blocks until released -- long
    enough for a test to type further commands while that turn is "running"."""

    def __init__(self, replies, gate: threading.Event):
        super().__init__(replies)
        self.gate = gate
        self.calls = 0

    def chat(self, messages, schema, **kw):
        self.calls += 1
        if self.calls == 1:
            self.gate.wait(5)
        return super().chat(messages, schema, **kw)


def _make_repl(monkeypatch, tmp_path, client) -> tuple[Repl, "os._wrap_close"]:
    r_fd, w_fd = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(r_fd))
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    repl = Repl(Config(), str(tmp_path))
    repl.manager.available = lambda: True
    return repl, w_fd


def _run_in_thread(repl: Repl) -> threading.Thread:
    t = threading.Thread(target=repl.run, daemon=True)
    t.start()
    return t


def _wait_until(predicate, timeout=5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def test_two_commands_queued_during_a_turn_run_in_order_after_it(
        monkeypatch, tmp_path, capsys):
    gate = threading.Event()
    client = _GatedClient([say("first done"), say("second done"), say("third done")], gate)
    repl, w_fd = _make_repl(monkeypatch, tmp_path, client)
    t = _run_in_thread(repl)

    _wait_until(lambda: repl._pump_thread is not None)
    os.write(w_fd, b"first\n")
    _wait_until(lambda: repl._turn_active.is_set())

    # Typed while the first turn is still running -- must queue, not run.
    os.write(w_fd, b"second\n")
    os.write(w_fd, b"third\n")
    _wait_until(lambda: repl._input_q.qsize() >= 2)
    assert list(repl._input_q.queue) == ["second", "third"]

    gate.set()  # let the first turn's model call return
    os.write(w_fd, b"/exit\n")
    t.join(timeout=5)
    assert not t.is_alive(), "Repl.run() did not return -- looks hung"

    out = capsys.readouterr().out
    assert out.index("first done") < out.index("second done") < out.index("third done")
    assert [m for m in client.seen] and len(client.seen) == 3


def test_cancel_mid_turn_keeps_the_queue(monkeypatch, tmp_path, capsys):
    """Ctrl-C during a turn (`Repl.run`'s `except KeyboardInterrupt: continue`
    around `self.agent.run(line)`) must not drop whatever was already
    queued, and the loop must carry on to run it next."""
    client = FakeClient([say("queued ran")])
    repl, w_fd = _make_repl(monkeypatch, tmp_path, client)

    calls = {"n": 0}
    real_run = repl.agent.run

    def flaky_run(line):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return real_run(line)

    monkeypatch.setattr(repl.agent, "run", flaky_run)
    t = _run_in_thread(repl)

    _wait_until(lambda: repl._pump_thread is not None)
    os.write(w_fd, b"cancel me\n")
    _wait_until(lambda: calls["n"] >= 1)
    # The queued-while-cancelling command below must survive the cancel.
    os.write(w_fd, b"queued while cancelling\n")
    _wait_until(lambda: repl._input_q.qsize() >= 1 or calls["n"] >= 2)
    os.write(w_fd, b"/exit\n")
    t.join(timeout=5)

    assert not t.is_alive(), "Repl.run() did not return -- looks hung"
    assert calls["n"] == 2, "the queued command must still run after the cancel"
    out = capsys.readouterr().out
    assert "interrupted -- queued commands are kept" in out
    assert "queued ran" in out


def test_approval_prompt_does_not_eat_a_queued_command(monkeypatch, tmp_path, capsys):
    """A command already queued ahead of a mid-turn approval prompt must
    stay queued; the approval's own y/n answer is read from a separate
    channel (`_approval_q`), fed only while `_awaiting_approval` is set."""
    client = FakeClient([say("noop")])
    repl, w_fd = _make_repl(monkeypatch, tmp_path, client)
    repl._start_pump()

    os.write(w_fd, b"queued command\n")
    _wait_until(lambda: repl._input_q.qsize() >= 1)

    def _answer_after_prompt_starts():
        _wait_until(lambda: repl._awaiting_approval.is_set())
        os.write(w_fd, b"y\n")

    threading.Thread(target=_answer_after_prompt_starts, daemon=True).start()
    from wilbur.tools.base import Approval
    outcome = repl._approve("run_bash", "echo hi")

    assert outcome is Approval.GRANTED
    assert list(repl._input_q.queue) == ["queued command"]


def test_typing_pauses_the_spinner_until_the_line_is_submitted(monkeypatch, tmp_path):
    """Regression for the owner-reported "typing mid-turn is invisible" bug:
    the spinner's ~0.12s redraw stomps whatever the user has typed on the
    same terminal line. The pump must pause it the moment a byte lands with
    no trailing newline yet (a line in progress), and resume it once Enter
    completes that line -- see `wilbur/ui.py`'s `Spinner` docstring."""
    from wilbur import ui as ui_mod

    client = FakeClient([say("noop")])
    repl, w_fd = _make_repl(monkeypatch, tmp_path, client)
    repl._start_pump()
    spinner = ui_mod.Spinner()
    repl._spinner = spinner

    os.write(w_fd, b"partial no newline yet")
    _wait_until(lambda: spinner._paused.is_set())

    os.write(w_fd, b"\n")
    _wait_until(lambda: not spinner._paused.is_set())
    assert list(repl._input_q.queue) == ["partial no newline yet"]


def test_pump_survives_sys_stdin_reassignment(monkeypatch, tmp_path):
    """A stray reference to the old `sys.stdin` file object must be kept
    alive for the pump's own lifetime. Losing it (using only the bare fd
    int after `.fileno()`) let a later `sys.stdin` reassignment garbage
    collect that object, close its fd, and hand the fd number to an
    unrelated later pipe -- this thread would then silently steal that
    pipe's bytes. Caught as flaky test ordering failures across the suite
    (216 passed intermittently dropping to 215 with a lost-input timeout)."""
    client = FakeClient([say("noop")])
    repl, w_fd = _make_repl(monkeypatch, tmp_path, client)
    repl._start_pump()
    _wait_until(lambda: repl._pump_thread is not None)

    # Simulate what pytest's own fixture teardown does between tests: drop
    # the only other reference to the stdin object the pump is reading.
    import gc
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    gc.collect()

    os.write(w_fd, b"still delivered\n")
    _wait_until(lambda: repl._input_q.qsize() >= 1)
    assert list(repl._input_q.queue) == ["still delivered"]


def test_queue_command_lists_and_clears(monkeypatch, tmp_path, capsys):
    client = FakeClient([say("noop")])
    repl, w_fd = _make_repl(monkeypatch, tmp_path, client)
    repl._input_q.put("a")
    repl._input_q.put("b")

    repl._queue_command("/queue")
    out = capsys.readouterr().out
    assert "2 queued" in out and "a" in out and "b" in out

    repl._queue_command("/queue clear")
    out = capsys.readouterr().out
    assert "2 command(s) dropped" in out
    assert repl._input_q.qsize() == 0


SIGNOFF = "THANK YOU FOR THE SLOP — MAY I HAVE ANOTHER?"


def test_signoff_only_after_the_last_queued_turn(monkeypatch, tmp_path, capsys):
    """The idle sign-off is an honest 'waiting on you' signal: not printed while
    queued commands remain, printed exactly once when the queue drains."""
    gate = threading.Event()
    client = _GatedClient([say("first done"), say("second done"), say("third done")], gate)
    repl, w_fd = _make_repl(monkeypatch, tmp_path, client)
    t = _run_in_thread(repl)
    _wait_until(lambda: repl._pump_thread is not None)
    os.write(w_fd, b"first\n")
    _wait_until(lambda: repl._turn_active.is_set())
    os.write(w_fd, b"second\n")
    os.write(w_fd, b"third\n")
    _wait_until(lambda: repl._input_q.qsize() >= 2)
    gate.set()
    _wait_until(lambda: len(client.seen) == 3 and not repl._turn_active.is_set())
    os.write(w_fd, b"/exit\n")
    t.join(timeout=5)
    out = capsys.readouterr().out
    assert out.count(SIGNOFF) == 1
    assert out.index("third done") < out.index(SIGNOFF)


def test_signoff_suppressed_when_disabled_or_limit_hit(monkeypatch, tmp_path, capsys):
    gate = threading.Event()
    gate.set()
    client = _GatedClient([say("done")], gate)
    repl, w_fd = _make_repl(monkeypatch, tmp_path, client)
    repl.config.idle_signoff = False
    t = _run_in_thread(repl)
    _wait_until(lambda: repl._pump_thread is not None)
    os.write(w_fd, b"go\n")
    _wait_until(lambda: len(client.seen) == 1 and not repl._turn_active.is_set())
    os.write(w_fd, b"/exit\n")
    t.join(timeout=5)
    assert SIGNOFF not in capsys.readouterr().out

    repl.config.idle_signoff = True
    repl._turn_hit_limit = True
    assert repl._is_idle() is False
    repl._turn_hit_limit = False
    assert repl._is_idle() is True
