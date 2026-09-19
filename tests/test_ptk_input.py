"""prompt_toolkit pump: the Claude-Code-style pinned prompt/toolbar.

`test_async_input.py` and `test_real_tty.py` drive the old fd-level cbreak
pump (`Repl._pump_stdin`), used off a real tty or when prompt_toolkit is
unavailable. These tests drive the new `Repl._ptk_pump` instead, using
prompt_toolkit's own `create_pipe_input`/`DummyOutput` test doubles rather
than a real tty -- the recommended way to test a prompt_toolkit application
(see prompt_toolkit's own test suite). They cover the same contract the old
pump had to keep: output during a turn doesn't clobber typed input, commands
typed mid-turn queue, and the approval flow round-trips through the same
`_approval_q`. Ctrl-C's `os.kill(getpid(), SIGINT)` re-delivery in
`Repl._ptk_pump` is exercised manually rather than here -- driving a real
SIGINT through prompt_toolkit's own event loop from a pipe-input test proved
flaky (prompt_toolkit's own crash handler intercepted it, "NOT DONE" in the
handoff).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import wilbur.agent as agent_mod
from wilbur.config import Config
from wilbur.repl import Repl
from wilbur.tools.base import Approval

from conftest import FakeClient, say


def _wait_until(predicate, timeout=5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def _join_pump(repl: Repl) -> None:
    """Wait for the pump thread to actually finish before the test ends --
    call after the `create_pipe_input()` `with` block has exited, so its
    `close()` has already sent the pump's `EOFError` exit path.

    prompt_toolkit's `run_in_terminal` keeps a single MODULE-LEVEL pending
    future to serialise nested calls across every `Application`, regardless
    of which thread or event loop created it -- not thread-local. A pump
    thread abandoned mid-prompt (the common case: a daemon thread still
    blocked in `session.prompt()` when the test function returns) can leave
    that future attached to its own now-dead event loop; the next test's
    pump then awaits it from a different loop and raises `RuntimeError: ...
    attached to a different loop`. Letting each pump run its own exit path
    to completion avoids that cross-test leak.
    """
    if repl._ptk_thread is not None:
        repl._ptk_thread.join(timeout=2.0)


def _make_repl(monkeypatch, tmp_path, client) -> Repl:
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    repl = Repl(Config(), str(tmp_path))
    repl.manager.available = lambda: True
    repl._use_ptk = True  # force the ptk path regardless of this test's own tty
    return repl


def test_ptk_pump_queues_a_typed_line(monkeypatch, tmp_path):
    client = FakeClient([say("noop")])
    repl = _make_repl(monkeypatch, tmp_path, client)

    with create_pipe_input() as pipe_input:
        repl._start_ptk_pump(pt_input=pipe_input, pt_output=DummyOutput())
        _wait_until(lambda: repl._ptk_thread is not None)
        pipe_input.send_text("hello world\n")
        _wait_until(lambda: repl._input_q.qsize() >= 1)
        assert list(repl._input_q.queue) == ["hello world"]
    _join_pump(repl)


def test_ptk_pump_two_commands_queue_during_a_turn(monkeypatch, tmp_path):
    """Same contract as `test_two_commands_queued_during_a_turn_run_in_order_
    after_it` in test_async_input.py, but through the ptk pump: lines typed
    while `_turn_active` is set must land in `_input_q` in order, and printed
    turn output must not stop the pump from reading further input (this is
    exactly what `patch_stdout` inside `_ptk_pump` exists to guarantee).

    The "queued (N)" notice itself goes through prompt_toolkit's own
    `patch_stdout` write path, which renders on its own schedule rather than
    synchronously with `print()` -- asserting on `capsys` output here would
    race that renderer, so this only checks the queue contents, which
    `_deliver_line` populates synchronously."""
    client = FakeClient([say("first done"), say("second done")])
    repl = _make_repl(monkeypatch, tmp_path, client)

    with create_pipe_input() as pipe_input:
        repl._start_ptk_pump(pt_input=pipe_input, pt_output=DummyOutput())
        _wait_until(lambda: repl._ptk_thread is not None)

        repl._turn_active.set()
        pipe_input.send_text("second\n")
        pipe_input.send_text("third\n")
        _wait_until(lambda: repl._input_q.qsize() >= 2)
        repl._turn_active.clear()

        assert list(repl._input_q.queue) == ["second", "third"]
    _join_pump(repl)


def test_ptk_pump_approval_flow_round_trips(monkeypatch, tmp_path):
    """`_approve()` blocks on `_approval_q`; the ptk pump must route a line
    there instead of `_input_q` while `_awaiting_approval` is set, exactly
    like the old pump's `_deliver_line` -- this is shared code, so the test
    is really proving the ptk pump calls into it correctly."""
    client = FakeClient([say("noop")])
    repl = _make_repl(monkeypatch, tmp_path, client)

    with create_pipe_input() as pipe_input:
        repl._start_ptk_pump(pt_input=pipe_input, pt_output=DummyOutput())
        _wait_until(lambda: repl._ptk_thread is not None)

        def _answer_after_prompt_starts():
            _wait_until(lambda: repl._awaiting_approval.is_set())
            pipe_input.send_text("y\n")

        threading.Thread(target=_answer_after_prompt_starts, daemon=True).start()
        outcome = repl._approve("run_bash", "echo hi")
        assert outcome is Approval.GRANTED
    _join_pump(repl)


def test_falls_back_to_the_old_pump_off_a_tty(monkeypatch, tmp_path):
    """Off a tty (pipes, CI, tests) `Repl` must still pick the old fd-level
    pump -- `_use_ptk` is only ever True on a real terminal."""
    client = FakeClient([say("noop")])
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    repl = Repl(Config(), str(tmp_path))
    assert repl._use_ptk is False


def test_ptk_disabled_by_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("WILBUR_NO_PTK", "1")
    client = FakeClient([say("noop")])
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    repl = Repl(Config(), str(tmp_path))
    assert repl._use_ptk is False
