"""TodoWrite tool: storage, rendering, and error handling for bad input."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools import build_registry
from wilbur.tools.base import Approval, ToolContext


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


@pytest.fixture
def tools():
    return build_registry()


def test_valid_list_is_stored_and_echoed(ctx, tools):
    tool = tools["todo_write"]
    out = tool.run({"todos": [
        {"task": "write tests", "status": "in_progress"},
        {"task": "run suite", "status": "pending"},
        {"task": "bump version", "status": "completed"},
    ]}, ctx)
    assert not out.is_error
    # The list is stored on the tool instance for later turns.
    assert tool.todos == [
        {"task": "write tests", "status": "in_progress"},
        {"task": "run suite", "status": "pending"},
        {"task": "bump version", "status": "completed"},
    ]
    # And echoed back with the status marks and a completion count.
    assert "[>] write tests" in out.output
    assert "[ ] run suite" in out.output
    assert "[x] bump version" in out.output
    assert "1/3 complete" in out.output


def test_missing_todos_key_is_an_error_not_a_raise(ctx, tools):
    tool = tools["todo_write"]
    out = tool.run({}, ctx)
    assert out.is_error
    assert "No valid todos" in out.output
    assert tool.todos == []


def test_all_invalid_items_is_an_error_not_a_raise(ctx, tools):
    tool = tools["todo_write"]
    out = tool.run({"todos": ["not a dict", 42, {"status": "pending"}]}, ctx)
    assert out.is_error
    assert "No valid todos" in out.output
    assert tool.todos == []


def test_invalid_status_falls_back_to_pending(ctx, tools):
    tool = tools["todo_write"]
    out = tool.run({"todos": [{"task": "one", "status": "bogus"}]}, ctx)
    assert not out.is_error
    assert tool.todos == [{"task": "one", "status": "pending"}]
    assert "[ ] one" in out.output


def test_multiple_in_progress_warns(ctx, tools):
    out = tools["todo_write"].run({"todos": [
        {"task": "a", "status": "in_progress"},
        {"task": "b", "status": "in_progress"},
    ]}, ctx)
    assert not out.is_error
    assert "more than one task is in_progress" in out.output


def test_plan_pushed_to_harness_state(ctx, tools):
    class FakeState:
        def __init__(self):
            self.plan = None
        def set_plan(self, plan):
            self.plan = plan

    state = FakeState()
    ctx.state = state
    todos = [{"task": "one", "status": "pending"}]
    out = tools["todo_write"].run({"todos": todos}, ctx)
    assert not out.is_error
    assert state.plan == todos
