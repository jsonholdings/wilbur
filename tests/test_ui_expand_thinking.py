"""U3 (ctrl-o / /expand the last cut tool result) and U5 (collapsed
"∴ Thinking..." block, ctrl-r / /thinking shows it). Both keys come from the
backend-neutral `ui.KEY_ACTIONS` table, which the ptk pump maps."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import wilbur.agent as agent_mod
from wilbur import render, ui
from wilbur.config import Config
from wilbur.repl import _SLASH_COMMANDS, HELP, Repl

from conftest import FakeClient, say
from test_ptk_input import _stop_pump, _wait_until

LONG = "\n".join(f"line {i}" for i in range(1, 21))
THINK = "First I read the spec.\nThen I check the tests.\nSECRET-ISH DETAIL"


@pytest.fixture(params=["plain", "colour"])
def mode(request, monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", request.param == "plain")
    return request.param


def _repl():
    repl = object.__new__(Repl)
    repl.stats = {"calls": 0, "in_tokens": 0, "out_tokens": 0, "seconds": 0.0}
    return repl


def _tool_end(repl, output, name="run_command", args=None):
    repl._on_event("tool_end", {"name": name, "error": False, "output": output,
                                "args": args or {"command": "x"}, "diff": None})


def _assistant(repl, thinking, content="done"):
    repl._on_event("assistant", {"content": content, "thinking": thinking, "tokens": 1,
                                 "seconds": 0.1})


# ------------------------------------------------------------ action table

def test_action_table_is_complete_and_wired_to_commands():
    keys = {key for key, _, _ in ui.KEY_ACTIONS.values()}
    assert keys == {"c-o", "c-r"}
    for key, command, help_text in ui.KEY_ACTIONS.values():
        assert command in _SLASH_COMMANDS and command in HELP and help_text
    assert ui.action_hint("expand_result") == "ctrl-o or /expand"


# ------------------------------------------------------------ U3

def test_cut_result_names_the_expand_key(mode, capsys):
    repl = _repl()
    _tool_end(repl, LONG)
    out = capsys.readouterr().out
    assert "+14 lines (ctrl-o or /expand)" in out
    assert "line 20" not in out


def test_expand_prints_the_whole_last_cut_result(mode, capsys):
    repl = _repl()
    _tool_end(repl, LONG)
    capsys.readouterr()
    assert repl._command("/expand") is None
    out = capsys.readouterr().out
    for i in (1, 7, 20):
        assert f"line {i}" in out
    assert "+14 lines" not in out
    assert ("\x1b" in out) == (mode == "colour")


def test_short_result_is_not_expandable(mode, capsys):
    """Control: nothing was cut, so there is nothing to expand."""
    repl = _repl()
    _tool_end(repl, "one\ntwo")
    capsys.readouterr()
    repl._command("/expand")
    assert "no cut tool result to expand" in capsys.readouterr().out


def test_read_summary_counts_as_cut(mode, capsys):
    repl = _repl()
    _tool_end(repl, LONG, name="read_file", args={"path": "notes.txt"})
    capsys.readouterr()
    repl._command("/expand")
    assert "line 20" in capsys.readouterr().out


# ------------------------------------------------------------ U5

def test_thinking_is_one_collapsed_dim_line(mode, capsys):
    repl = _repl()
    _assistant(repl, THINK)
    out = capsys.readouterr().out
    assert "∴ Thinking... (3 lines, ctrl-r or /thinking)" in out
    assert "SECRET-ISH DETAIL" not in out
    assert (ui.DIM in out) == (mode == "colour")


def test_thinking_command_shows_it_whole(mode, capsys):
    repl = _repl()
    _assistant(repl, THINK)
    capsys.readouterr()
    repl._command("/thinking")
    out = capsys.readouterr().out
    assert "First I read the spec." in out and "SECRET-ISH DETAIL" in out


def test_no_thinking_no_block(mode, capsys):
    """Control: a reply with no reasoning prints no block at all."""
    repl = _repl()
    _assistant(repl, "")
    assert "Thinking" not in capsys.readouterr().out
    repl._command("/thinking")
    assert "no reasoning in the last reply" in capsys.readouterr().out


def test_expanded_thinking_wraps_to_width(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", True)
    monkeypatch.setattr(ui, "width", lambda: 40)
    block = render.thinking_block("word " * 40, expanded=True)
    assert all(ui.visible_width(line) <= 40 for line in block.splitlines())
    assert block.count("word") == 40


# ------------------------------------------------------------ ptk keys

@pytest.mark.parametrize("key,action", [("\x0f", "expand_result"), ("\x12", "show_thinking")])
def test_ptk_key_runs_its_action(monkeypatch, tmp_path, key, action):
    monkeypatch.setattr(agent_mod, "OllamaClient", FakeClient([say("noop")]))
    repl = Repl(Config(), str(tmp_path))
    repl.manager.available = lambda: True
    repl._use_ptk = True
    ran = []
    monkeypatch.setattr(repl, "_run_action", ran.append)
    with create_pipe_input() as pipe_input:
        repl._start_ptk_pump(pt_input=pipe_input, pt_output=DummyOutput())
        _wait_until(lambda: repl._ptk_thread is not None)
        pipe_input.send_text("typed")
        pipe_input.send_text(key)
        _wait_until(lambda: ran == [action])
        pipe_input.send_text("\n")
        _wait_until(lambda: repl._input_q.qsize() >= 1)
        assert list(repl._input_q.queue) == ["typed"]  # the key kept the typed line
        pipe_input.send_text("\x15")  # ctrl-u: empty prompt so ctrl-d exits
        _stop_pump(repl, pipe_input)


def test_streaming_holds_the_thinking_line_while_the_prompt_owns_it(monkeypatch, capsys):
    """With streaming on, the block goes through the live channel: held
    while an approval prompt owns the terminal, written once it is free."""
    import threading
    monkeypatch.setattr(ui, "_NO_COLOR", True)
    repl = _repl()
    repl._live_on, repl._live, repl._live_pending = True, None, ""
    repl._awaiting_approval = threading.Event()
    repl._awaiting_approval.set()
    _assistant(repl, THINK)
    assert "Thinking" not in capsys.readouterr().out
    assert "∴ Thinking..." in repl._live_pending
    repl._awaiting_approval.clear()
    repl._live_flush()
    assert "∴ Thinking... (3 lines" in capsys.readouterr().out
