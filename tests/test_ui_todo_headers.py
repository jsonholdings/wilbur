"""U2 (2.65.0): the todo checklist (☐ / bold in progress / ☒ struck through),
shown again only when the plan changes, and Claude-Code-style tool headers
`Read(path)` with a `Read N lines` summary."""
from __future__ import annotations

import pytest

from wilbur import ui
from wilbur.repl import Repl

PLAN = [{"task": "read the spec", "status": "completed"},
        {"task": "write the code", "status": "in_progress"},
        {"task": "run the tests", "status": "pending"}]


@pytest.fixture
def plain(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", True)


@pytest.fixture
def colour(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", False)


def _repl():
    repl = object.__new__(Repl)
    repl.stats = {"calls": 0, "in_tokens": 0, "out_tokens": 0, "seconds": 0.0}
    return repl


def _todo_end(repl, todos):
    repl._on_event("tool_end", {"name": "todo_write", "error": False, "output": "x",
                                "args": {"todos": todos}, "diff": None})


def test_checklist_marks_each_state_plain(plain):
    out = ui.todo_checklist(PLAN).splitlines()
    assert out[0].endswith("☒ read the spec") and ui.BRANCH in out[0]
    assert out[1].endswith("☐ write the code")
    assert out[2].endswith("☐ run the tests")
    assert "\x1b" not in "".join(out)


def test_checklist_strikes_completed_and_bolds_in_progress(colour):
    rows = ui.todo_checklist(PLAN).splitlines()
    assert ui.STRIKE in rows[0] and ui.STRIKE not in rows[1] + rows[2]
    assert ui.BOLD + "☐ write the code" in rows[1]
    assert ui.BOLD not in rows[2]


def test_checklist_is_reprinted_only_when_the_plan_changes(plain, capsys):
    repl = _repl()
    _todo_end(repl, PLAN)
    first = capsys.readouterr().out
    assert "☐ write the code" in first
    _todo_end(repl, [dict(t) for t in PLAN])
    assert capsys.readouterr().out == ""
    changed = [dict(t) for t in PLAN]
    changed[1]["status"] = "completed"
    _todo_end(repl, changed)
    assert "☒ write the code" in capsys.readouterr().out


def test_tool_headers_use_claude_code_labels(plain):
    assert ui.tool_call("read_file", "src/a.py").endswith("Read(src/a.py)")
    assert "Bash(ls)" in ui.tool_call("run_bash", "ls")
    assert ui.tool_call("todo_write", "").endswith("Update Todos")
    assert "mcp_thing(x)" in ui.tool_call("mcp_thing", "x")   # control: unknown keeps its name


def test_read_file_result_is_a_line_count_summary(plain, capsys):
    repl = _repl()
    output = "\n".join(f"{n:6d}\tline {n}" for n in range(1, 121))
    repl._on_event("tool_end", {"name": "read_file", "error": False, "output": output,
                                "args": {"path": "a.py"}, "diff": None, "seconds": 0.2})
    out = capsys.readouterr().out
    assert "Read 120 lines" in out and "line 7" not in out


def test_read_file_error_and_empty_file_keep_the_text(plain, capsys):
    repl = _repl()
    repl._on_event("tool_end", {"name": "read_file", "error": True,
                                "output": "File does not exist: x", "args": {"path": "x"}})
    repl._on_event("tool_end", {"name": "read_file", "error": False,
                                "output": "[x is empty]", "args": {"path": "x"}})
    out = capsys.readouterr().out
    assert "File does not exist: x" in out and "[x is empty]" in out and "Read " not in out
