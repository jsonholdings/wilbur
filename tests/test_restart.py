"""/restart (owner, 2026-10-27): reload Wilbur's code after an update and
resume the same session in a new process. Driven through the real Repl
against FakeClient; the exec itself is stubbed. No model, no GPU."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.__main__ as main_mod
from wilbur import session as sessions
from wilbur.config import Config

from conftest import say
from test_runstate import _drive, _repl


class _GpuStub:
    """Records how the session ends; every other call is a no-op."""

    def __init__(self):
        self.unload = None

    def end_session(self, unload=True):
        self.unload = unload

    def __getattr__(self, name):
        return lambda *a, **k: None


def test_restart_saves_the_session_and_asks_for_a_resume(monkeypatch, tmp_path):
    repl = _repl(monkeypatch, tmp_path, [say("hello back")])
    _drive(repl, ["hello", "/restart"])
    assert repl.restart_requested
    saved = sessions.load(repl.session_id)
    assert saved is not None
    assert any(m.get("content") == "hello back" for m in saved["messages"])
    argv = repl.restart_argv()
    assert argv[:3] == [sys.executable, "-I", "-c"]
    assert argv[argv.index("--resume") + 1] == repl.session_id
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    assert argv[argv.index("--approve") + 1] == "auto-edit"


def test_restart_before_any_turn_still_leaves_a_session_to_resume(monkeypatch, tmp_path):
    # Turns save as they go; a restart typed first has nothing saved yet, and
    # --resume would refuse a session that does not exist.
    repl = _repl(monkeypatch, tmp_path, [])
    _drive(repl, ["/restart"])
    assert sessions.load(repl.session_id) is not None


def test_queued_lines_dropped_by_a_restart_are_named(monkeypatch, tmp_path, capsys):
    repl = _repl(monkeypatch, tmp_path, [])
    _drive(repl, ["/restart", "fix the README"])
    assert "1 queued line(s) dropped" in capsys.readouterr().out


def test_exit_is_not_a_restart(monkeypatch, tmp_path):
    repl = _repl(monkeypatch, tmp_path, [])
    _drive(repl, ["/exit"])
    assert not repl.restart_requested


def test_restart_keeps_the_model_resident_and_exit_unloads_it(monkeypatch, tmp_path):
    for line, unload in (("/restart", False), ("/exit", True)):
        repl = _repl(monkeypatch, tmp_path, [])
        repl._gp = _GpuStub()
        repl._begin_gpu_session = lambda: None
        _drive(repl, [line])
        assert repl._gp.unload is unload


def test_settings_changed_in_the_session_are_carried_over(monkeypatch, tmp_path):
    saved = Config.load()

    def configure(cfg):
        cfg.model = "other:7b"
        cfg.num_ctx = saved.num_ctx * 2

    repl = _repl(monkeypatch, tmp_path, [], configure=configure)
    argv = repl.restart_argv()
    assert argv[argv.index("-m") + 1] == "other:7b"
    assert argv[argv.index("-c", 4) + 1] == str(saved.num_ctx * 2)  # [2] is python -c
    assert "--url" not in argv  # unchanged settings come from config.json, not the flags


def test_exec_restart_runs_the_argv(monkeypatch):
    ran = []
    monkeypatch.setattr(main_mod.os, "execv", lambda path, argv: ran.append((path, argv)))
    argv = [sys.executable, "-I", "-m", "wilbur", "--resume", "s1"]
    assert main_mod._exec_restart(argv, None) == 0
    assert ran == [(sys.executable, argv)]


def test_failed_exec_says_how_to_resume(monkeypatch, capsys):
    def fail(path, argv):
        raise OSError("no such file")

    monkeypatch.setattr(main_mod.os, "execv", fail)
    assert main_mod._exec_restart(["/nonexistent", "--resume", "s1"], None) == 1
    assert "wilbur --resume s1" in capsys.readouterr().err


def _launch(monkeypatch, tmp_path, *args):
    # The real restart command line, run from "/" so the working directory
    # cannot supply the package: it must import from a plain checkout (the
    # public mirror's CI has no install) as well as from an install.
    repl = _repl(monkeypatch, tmp_path, [])
    argv = repl.restart_argv()
    return subprocess.run(argv[:4] + list(args), capture_output=True, text=True,
                          timeout=60, cwd="/")


def test_restart_command_line_starts_this_wilbur(monkeypatch, tmp_path):
    from wilbur import __version__
    out = _launch(monkeypatch, tmp_path, "--version")
    assert out.returncode == 0 and out.stdout.strip() == f"wilbur {__version__}"


def test_restart_command_line_knows_the_approve_flag(monkeypatch, tmp_path):
    out = _launch(monkeypatch, tmp_path, "--help")
    assert out.returncode == 0 and "--approve" in out.stdout
