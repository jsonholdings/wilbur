import io
import sys
import pytest
from wilbur import runstate, repl as repl_mod


class TTY(io.StringIO):
    def isatty(self):
        return True


class Cfg:
    set_title = True
    model = "qwen3"


class Fake:
    config = Cfg()
    cwd = "/srv/proj"
    session_id = "s1"
    _start_inbox_watch = lambda self: None
    _stop_inbox_watch = lambda self: None
    _persist_subagents = lambda self, rows: None


def _run(monkeypatch, buf, term="xterm-256color", fail=False):
    monkeypatch.setenv("TERM", term)
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(runstate, "remove", lambda sid: None)
    f = Fake()
    def loop():
        if fail:
            raise RuntimeError("boom")
        return 0
    f._run_loop = loop
    return repl_mod.Repl.run(f)


def test_sets_on_start_and_restores(monkeypatch):
    buf = TTY()
    assert _run(monkeypatch, buf) == 0
    out = buf.getvalue()
    assert out.startswith(runstate.TITLE_PUSH)
    assert "\x1b]2;\U0001F416 Wilbur \u00b7 proj \u00b7 qwen3\x07" in out
    assert "\x1b]1;" in out
    assert out.endswith(runstate.TITLE_POP)


def test_restores_on_exception(monkeypatch):
    buf = TTY()
    with pytest.raises(RuntimeError):
        _run(monkeypatch, buf, fail=True)
    assert buf.getvalue().endswith(runstate.TITLE_POP)


def test_no_escapes_off_tty(monkeypatch):
    buf = io.StringIO()
    _run(monkeypatch, buf)
    assert buf.getvalue() == ""


def test_no_escapes_for_dumb_term(monkeypatch):
    buf = TTY()
    _run(monkeypatch, buf, term="dumb")
    assert buf.getvalue() == ""
