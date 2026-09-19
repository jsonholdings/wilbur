"""Real-tty check for the fd-level stdin pump (`Repl._pump_stdin`).

`test_async_input.py` drives the pump over `os.pipe()`, which is never a
tty -- `isatty()` is False, so the cbreak/echo branch in `_pump_stdin` is
never exercised there. This file drives it over a real `pty.openpty()` pair
instead, so `is_tty` is True and the code path that switches the terminal to
cbreak mode and echoes each byte back itself actually runs.
"""
from __future__ import annotations

import os
import pty
import sys
import termios
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.agent as agent_mod
from wilbur.config import Config
from wilbur.repl import Repl

from conftest import FakeClient, say


def _wait_until(predicate, timeout=5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def _make_pty_repl(monkeypatch, tmp_path, client):
    controller_fd, follower_fd = pty.openpty()
    # A real terminal has stdin and stdout as the same device, and the pump's
    # echo-on-cbreak path writes to sys.stdout -- wire it to the same pty so
    # the echo is observable on the controller side, same as a real terminal.
    follower_in = os.fdopen(follower_fd, "rb", buffering=0)
    follower_out = os.fdopen(os.dup(follower_fd), "w", buffering=1)
    monkeypatch.setattr(sys, "stdin", follower_in)
    monkeypatch.setattr(sys, "stdout", follower_out)
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    repl = Repl(Config(), str(tmp_path))
    repl.manager.available = lambda: True
    return repl, controller_fd


def _read_all(fd, timeout=1.0) -> bytes:
    import select
    out = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    return out


def _wait_for_cbreak(fd, timeout=2.0) -> None:
    """`_start_pump` returns as soon as the thread object exists, before the
    thread body has actually called `tty.setcbreak` -- poll for the real
    effect instead of racing on thread existence alone."""
    _wait_until(lambda: not (termios.tcgetattr(fd)[3] & termios.ICANON), timeout)


def test_typed_keystrokes_echo_on_a_real_tty(monkeypatch, tmp_path):
    """On a real tty the pump must echo each byte back itself (cbreak turns
    off the kernel's own echo) rather than relying on the kernel."""
    client = FakeClient([say("noop")])
    repl, controller_fd = _make_pty_repl(monkeypatch, tmp_path, client)
    repl._start_pump()
    _wait_for_cbreak(sys.stdin.fileno())

    os.write(controller_fd, b"hi")
    echoed = _read_all(controller_fd, timeout=1.0)
    assert b"hi" in echoed, f"typed bytes were not echoed back: {echoed!r}"


def test_enter_queues_the_line_on_a_real_tty(monkeypatch, tmp_path):
    client = FakeClient([say("noop")])
    repl, controller_fd = _make_pty_repl(monkeypatch, tmp_path, client)
    repl._start_pump()
    _wait_for_cbreak(sys.stdin.fileno())

    os.write(controller_fd, b"hello world\r")
    _wait_until(lambda: repl._input_q.qsize() >= 1)
    assert list(repl._input_q.queue) == ["hello world"]


def test_terminal_mode_restored_on_pump_exit(monkeypatch, tmp_path):
    """`_pump_stdin`'s `finally` must restore the original termios settings
    it saved before switching to cbreak, on a clean EOF (Ctrl-D at an empty
    line) exit."""
    client = FakeClient([say("noop")])
    repl, controller_fd = _make_pty_repl(monkeypatch, tmp_path, client)
    follower_fd = sys.stdin.fileno()
    before = termios.tcgetattr(follower_fd)

    repl._start_pump()
    _wait_for_cbreak(follower_fd)

    os.write(controller_fd, b"\x04")  # Ctrl-D at an empty line -> pump returns
    _wait_until(lambda: repl._pump_thread is not None and not repl._pump_thread.is_alive())

    after = termios.tcgetattr(follower_fd)
    assert after == before, "termios settings were not restored on pump exit"


def test_terminal_mode_restored_on_ctrl_c(monkeypatch, tmp_path):
    """A SIGINT delivered on the main thread while the pump thread is reading
    must not by itself disturb the tty mode -- the pump keeps running in
    cbreak (Repl.run() catches KeyboardInterrupt around `_next_line()` and
    keeps the pump alive so queued commands survive, per
    `test_cancel_mid_turn_keeps_the_queue`). Only the pump's own exit path
    restores the terminal, and it still does so correctly after a SIGINT was
    delivered in the meantime."""
    import signal

    client = FakeClient([say("noop")])
    repl, controller_fd = _make_pty_repl(monkeypatch, tmp_path, client)
    follower_fd = sys.stdin.fileno()
    before = termios.tcgetattr(follower_fd)

    repl._start_pump()
    _wait_for_cbreak(follower_fd)

    try:
        os.kill(os.getpid(), signal.SIGINT)
    except KeyboardInterrupt:
        pass  # delivered to this (main) thread, not the pump thread

    # The pump thread must be unaffected: still alive, still in cbreak.
    assert repl._pump_thread.is_alive()
    during = termios.tcgetattr(follower_fd)
    assert not (during[3] & termios.ICANON), "SIGINT disturbed the pump's cbreak mode"

    os.write(controller_fd, b"\x04")
    _wait_until(lambda: repl._pump_thread is not None and not repl._pump_thread.is_alive())
    after = termios.tcgetattr(follower_fd)
    assert after == before, "termios settings were not restored after Ctrl-C + exit"


def test_typing_during_a_turn_is_not_erased_by_the_spinner(monkeypatch, tmp_path):
    """Regression (owner report, 2026-09-18): during a turn every keystroke
    vanished because the pump echoed the byte and THEN paused the spinner,
    and pause() clears the line. The clear must come before the echo, once."""
    import wilbur.ui as ui
    monkeypatch.setattr(ui, "_NO_COLOR", False)
    client = FakeClient([say("noop")])
    repl, controller_fd = _make_pty_repl(monkeypatch, tmp_path, client)
    repl._spinner = ui.Spinner()
    repl._start_pump()
    _wait_for_cbreak(sys.stdin.fileno())

    for ch in (b"h", b"e", b"y"):
        os.write(controller_fd, ch)
        time.sleep(0.15)
    out = _read_all(controller_fd, timeout=1.0)
    assert b"hey" in out.replace(b"\r\x1b[2K", b""), f"typed bytes missing: {out!r}"
    last_clear = out.rfind(b"\x1b[2K")
    assert last_clear < out.find(b"h"), f"line cleared after typing started: {out!r}"
