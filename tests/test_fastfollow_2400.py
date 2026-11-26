"""wilbur-2400-fastfollow (2.61.0): write signatures carry a content digest,
a new task clears the failure ledger, a typed line resets the runaway streak,
and Config.validate checks integer knobs at load."""
from __future__ import annotations

import json

import pytest

import wilbur.config as config_mod
from wilbur.agent import Agent
from wilbur.config import Config
from wilbur.llm import ToolCall


def _sig(name, **args):
    return Agent._arg_signature(ToolCall(name=name, arguments=args))


def test_same_path_different_content_is_a_different_write():
    a = _sig("write_file", path="x.py", content="print(1)\n")
    b = _sig("write_file", path="x.py", content="print(2)\n")
    assert a != b
    assert _sig("edit_file", path="x.py", old_string="a", new_string="b") != \
        _sig("edit_file", path="x.py", old_string="a", new_string="c")


def test_identical_write_keeps_its_signature_control():
    assert _sig("write_file", path="x.py", content="same") == \
        _sig("write_file", path="x.py", content="same")
    # non-write tools are unchanged: path only
    assert _sig("read_file", path="x.py") == "path=x.py"


def test_begin_task_clears_the_failure_ledger(make_agent):
    a, _ = make_agent([])
    a.state.record_failure("run_bash(command=make)", "exit 2")
    a._big_content_streak = 1
    a.begin_user_turn()
    assert a.state.failures, "control: a typed line keeps the ledger (2.51.1)"
    assert a._big_content_streak == 0
    a.begin_task()
    assert a.state.failures == []


def test_loop_first_turn_starts_a_task(make_agent):
    from conftest import say
    from wilbur.loop import GOAL_COMPLETE, run_goal_loop
    a, _ = make_agent([say(f"done {GOAL_COMPLETE}")])
    a.state.record_failure("run_bash(command=make)", "exit 2")
    run_goal_loop(a, "goal", check=None, max_turns=1)
    assert not any(f.what == "run_bash(command=make)" for f in a.state.failures)


def test_config_validate_rejects_non_numbers(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"tool_failure_stop_at": "three"}))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    with pytest.raises(ValueError, match="tool_failure_stop_at"):
        Config.load()


def test_config_validate_clamps_to_minimum(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"tool_failure_block_at": 0, "auto_continue_max": -4}))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    cfg = Config.load()
    assert cfg.tool_failure_block_at == 1 and cfg.auto_continue_max == 0
