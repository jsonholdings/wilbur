"""stopped-state-is-visible (BACKLOG, owner 2026-10-16): run.json is the
authoritative run state, a stop is loud on a tty, and `wilbur status --check`
returns 0/1/2. Driven through the real Repl against FakeClient; no model."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.agent as agent_mod
from wilbur import config as config_mod
from wilbur import runstate, status
from wilbur.config import Config
from wilbur.repl import Repl

from conftest import FakeClient, say, call


def _write(name):
    from wilbur.llm import Reply
    return Reply(content="", tool_calls=[call("write_file", path=name, content=name)])


def _repl(monkeypatch, tmp_path, replies, configure=None):
    monkeypatch.setattr(agent_mod, "OllamaClient", FakeClient(replies))
    r_fd, w_fd = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(r_fd))
    os.close(w_fd)
    cfg = Config()
    if configure:
        configure(cfg)
    repl = Repl(cfg, str(tmp_path))
    repl.manager.available = lambda: True
    repl._use_ptk = False
    repl.approve_mode = "auto-edit"
    return repl


def _drive(repl, lines, timeout=10):
    for line in lines:
        repl._input_q.put(line)
    t = threading.Thread(target=repl.run, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive()


# --- run.json -----------------------------------------------------------------

def test_write_is_atomic_and_read_back(tmp_path):
    p = runstate.write("s1", state=runstate.WORKING, cwd="/x", model="m", rounds=2)
    assert p is not None and p.name == "s1.run.json"
    assert not list(p.parent.glob("*.tmp"))
    data = runstate.read("s1")
    assert data["state"] == "working" and data["rounds"] == 2 and data["pid"] == os.getpid()
    for key in ("pid", "session_id", "cwd", "state", "reason", "since",
                "last_progress_at", "rounds", "model", "version"):
        assert key in data
    assert runstate.remove("s1") and runstate.read("s1") is None
    assert runstate.remove("s1") is False


def test_dead_pid_reads_as_dead(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    runstate.write("gone", state=runstate.WORKING, pid=proc.pid)
    data = runstate.read("gone")
    assert data["state"] == "dead" and data["original_state"] == "working"


def test_recycled_pid_that_is_not_wilbur_reads_as_dead(tmp_path):
    if not Path("/proc/self/cmdline").exists():
        pytest.skip("no /proc")
    proc = subprocess.Popen(["sleep", "30"])
    try:
        runstate.write("recycled", state=runstate.WORKING, pid=proc.pid)
        assert runstate.read("recycled")["state"] == "dead"
    finally:
        proc.kill()
        proc.wait()


def test_reason_from_decision_maps_keepgoing_reasons():
    assert runstate.reason_from_decision("asked you a question") == "question"
    assert runstate.reason_from_decision("empty reply") == "empty_reply"
    assert runstate.reason_from_decision("cancelled") == "user_interrupt"
    assert runstate.reason_from_decision("all items done") == "done"
    assert set(runstate.IDLE_EXIT_CODES) == set(runstate.STOP_REASONS)


# --- the Repl writes it ---------------------------------------------------------

def test_turn_end_writes_stopped_with_reason_and_exit_removes(monkeypatch, tmp_path):
    repl = _repl(monkeypatch, tmp_path, [say("Should b.txt be JSON or YAML?")])
    seen = []
    real = runstate.write

    def spy(session_id, **kw):
        seen.append((kw["state"], kw.get("reason")))
        return real(session_id, **kw)
    monkeypatch.setattr(runstate, "write", spy)
    _drive(repl, ["make b.txt"])
    assert ("working", None) in seen
    assert ("stopped", "question") in seen
    assert seen[0] == ("waiting_input", None)
    assert runstate.read(repl.session_id) is None  # removed on clean exit


def test_horizon_stop_reason(monkeypatch, tmp_path):
    from wilbur import horizon as hz
    repl = _repl(monkeypatch, tmp_path, [_write("a.txt"), say("Next, I'll do b.")])
    clock = iter([0.0, 10_000.0, 10_000.0, 10_000.0])
    real = repl._keep_going
    monkeypatch.setattr(repl, "_keep_going", lambda reply: real(reply, clock=lambda: next(clock)))
    _drive(repl, ["make a.txt, work for 5 minutes"])
    assert repl._stop_reason == "horizon"


def test_stuck_and_caps_have_their_own_reasons(monkeypatch, tmp_path):
    replies = [_write("a.txt")] + [say("Next, I'll keep going.")] * 6

    def cap1(cfg):
        cfg.auto_continue_max = 1
    repl = _repl(monkeypatch, tmp_path, replies, configure=cap1)
    _drive(repl, ["go"])
    assert repl._stop_reason == "count_cap"


# --- loud stop --------------------------------------------------------------------

class _TTY(io.StringIO):
    def isatty(self):
        return True


def _stop_out(monkeypatch, tmp_path, tty, **cfg_kw):
    def conf(cfg):
        for k, v in cfg_kw.items():
            setattr(cfg, k, v)
    repl = _repl(monkeypatch, tmp_path, [], configure=conf)
    buf = _TTY() if tty else io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    repl._enter_stopped("horizon")
    return buf.getvalue()


def test_stop_is_loud_on_a_tty(monkeypatch, tmp_path):
    out = _stop_out(monkeypatch, tmp_path, tty=True)
    assert "STOPPED — horizon — idle since" in out
    assert "\a" in out and "\x1b]0;wilbur: stopped (horizon)" in out


def test_no_bell_or_title_off_a_tty(monkeypatch, tmp_path):
    out = _stop_out(monkeypatch, tmp_path, tty=False)
    assert "STOPPED" in out and "\a" not in out and "\x1b]0;" not in out


def test_stop_bell_and_set_title_switches(monkeypatch, tmp_path):
    out = _stop_out(monkeypatch, tmp_path, tty=True, stop_bell=False, set_title=False)
    assert "STOPPED" in out and "\a" not in out and "\x1b]0;" not in out
    out = _stop_out(monkeypatch, tmp_path, tty=True, stop_bell=True, set_title=False)
    assert "\a" in out and "\x1b]0;" not in out


# --- idle exit ----------------------------------------------------------------------

def _idle_run(monkeypatch, tmp_path, line, minutes, with_exit=True):
    def conf(cfg):
        cfg.idle_exit_minutes = minutes
    repl = _repl(monkeypatch, tmp_path, [say("All done.")], configure=conf)
    # Keep stdin OPEN (write end held), so only the idle exit can end the run.
    r_fd, w_fd = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(r_fd))
    repl._held_stdin_fd = w_fd
    repl._input_q.put(line)
    real_within = repl._next_line_within
    monkeypatch.setattr(repl, "_next_line_within", lambda seconds: real_within(0.05))
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("rc", repl.run()), daemon=True)
    t.start()
    if with_exit:
        t.join(15)
    else:
        # Wait until the turn has really stopped, then give the (shortened)
        # idle wait many chances to fire, so "still alive" means something.
        deadline = time.time() + 15
        while repl._stopped_since is None and time.time() < deadline:
            time.sleep(0.02)
        assert repl._stopped_since is not None
        time.sleep(0.5)
    return repl, out.get("rc"), t


def _stop_pump(repl):
    """EOF on the held stdin, then wait for the pump thread to finish, so the
    leaked-thread check in conftest never races it."""
    os.close(repl._held_stdin_fd)
    pump = getattr(repl, "_pump_thread", None)
    if pump is not None:
        pump.join(15)
        assert not pump.is_alive()


def _release(repl, t):
    _stop_pump(repl)  # EOF: the pump hands the prompt a None
    t.join(15)
    assert not t.is_alive()


def test_idle_exit_fires_after_a_horizon_stop(monkeypatch, tmp_path, capsys):
    repl, rc, t = _idle_run(monkeypatch, tmp_path, "tidy up, work for 5 minutes", 1)
    assert not t.is_alive() and rc == runstate.IDLE_EXIT_CODES["done"]
    assert "idle exit: stopped (done)" in capsys.readouterr().out
    _stop_pump(repl)
    assert (config_mod.SESSION_DIR / f"{repl.session_id}.json").exists()


def test_idle_exit_never_fires_without_a_horizon(monkeypatch, tmp_path, capsys):
    repl, rc, t = _idle_run(monkeypatch, tmp_path, "tidy up", 1, with_exit=False)
    assert t.is_alive()  # still at its prompt: never auto-exited
    assert repl._idle_exit_due() is None
    _release(repl, t)
    assert "idle exit" not in capsys.readouterr().out


# --- wilbur status --check --------------------------------------------------------------

def test_status_check_ok_when_nothing_running(capsys):
    assert status.status_main(["--check"]) == 0
    assert "no runs" in capsys.readouterr().out


def test_status_check_hung_working_run_is_1(capsys):
    runstate.write("hung", state=runstate.WORKING, last_progress_at=time.time() - 3600)
    assert status.status_main(["--check"]) == 1
    runstate.write("hung", state=runstate.WORKING)
    assert status.status_main(["--check"]) == 0


def test_status_check_dead_file_is_1(capsys):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    runstate.write("dead", state=runstate.STOPPED, reason="horizon", pid=proc.pid)
    assert status.status_main(["--check", "--json"]) == 1
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["state"] == "dead" and rows[0]["reason"] == "horizon"


def test_status_check_unreadable_dir_is_unknown_2(monkeypatch, tmp_path, capsys):
    if os.geteuid() == 0:
        pytest.skip("root reads anything")
    locked = tmp_path / "locked"
    locked.mkdir()
    monkeypatch.setattr(config_mod, "SESSION_DIR", locked)
    locked.chmod(0)
    try:
        assert status.status_main(["--check"]) == 2
    finally:
        locked.chmod(0o700)


def test_status_never_creates_the_sessions_dir(monkeypatch, tmp_path):
    missing = tmp_path / "nope"
    monkeypatch.setattr(config_mod, "SESSION_DIR", missing)
    assert status.status_main(["--check"]) == 0 and not missing.exists()


def test_status_subcommand_is_wired():
    from wilbur.__main__ import main
    assert main(["status", "--check"]) == 0
