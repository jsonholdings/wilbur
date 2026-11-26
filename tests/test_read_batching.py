"""2.59.0: batch independent tool calls (trace-2572: rounds ~= calls, e.g.
26_config_key_migration rep 0 read 4 files in 4 consecutive rounds).

- the static system prompt and the read/search tool descriptions ask for
  independent calls in ONE reply;
- several read-only calls in one reply run concurrently, results in order;
- a write or command in the same reply stays sequential: reads after it see
  its effect and never overlap it;
- glob resolves a relative path under the project root;
- the outside-project hint no longer names an unrelated same-named file.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from conftest import call, say
from wilbur import agent as agent_mod
from wilbur.agent import SYSTEM_PROMPT
from wilbur.config import Config
from wilbur.llm import Reply
from wilbur.tools import build_registry, hints
from wilbur.tools.base import Approval, ToolContext


class _Instrumented:
    """Wraps a real tool; records overlap with other instrumented calls."""

    def __init__(self, tool, log, lock, active, barrier=None):
        self._tool, self._log, self._lock, self._active = tool, log, lock, active
        self._barrier = barrier
        self.name, self.schema = tool.name, tool.schema
        self.description = tool.description
        self.read_only = getattr(tool, "read_only", False)

    def run(self, args, ctx):
        with self._lock:
            self._active[self.name] = self._active.get(self.name, 0) + 1
            overlap = {k: v for k, v in self._active.items() if v}
            self._log.append(("start", self.name, args.get("path"), dict(overlap),
                              threading.current_thread().name))
        try:
            if self._barrier is not None:
                self._barrier.wait(timeout=5)
            return self._tool.run(args, ctx)
        finally:
            with self._lock:
                self._active[self.name] -= 1


def _instrument(a, names, barrier=None):
    log, lock, active = [], threading.Lock(), {}
    for name in names:
        a.registry[name] = _Instrumented(a.registry[name], log, lock, active, barrier)
    return log


def _tool_results(a):
    return [m for m in a.messages if m.get("role") == "tool"]


def test_prompt_and_descriptions_ask_for_batched_calls():
    assert "Run one tool at a time" not in SYSTEM_PROMPT
    assert "Batch independent calls in ONE reply" in SYSTEM_PROMPT
    assert "test once after edits" in SYSTEM_PROMPT
    reg = build_registry()
    for name in ("read_file", "glob", "list_files"):
        assert "parallel" in reg[name].description, name
    assert "once for the batch" in reg["run_tests"].description


def test_prompt_guidance_is_in_the_static_system_message(make_agent):
    a, _ = make_agent([say("ok")])
    assert a.messages[0]["role"] == "system"
    assert "Batch independent calls in ONE reply" in a.messages[0]["content"]


def test_multi_read_reply_runs_concurrently_and_returns_in_order(make_agent, tmp_path):
    for i in range(3):
        (tmp_path / f"f{i}.py").write_text(f"value_{i} = {i}\n")
    reads = [call("read_file", path=f"f{i}.py") for i in range(3)]
    a, _ = make_agent([Reply(content="", tool_calls=reads), say("done")])
    # A 3-party barrier: if the reads ran one after another the first would
    # time out (BrokenBarrierError) and its result would be an error.
    log = _instrument(a, ["read_file"], barrier=threading.Barrier(3))
    assert a.run("read them") == "done"
    results = _tool_results(a)
    assert len(results) == 3
    for i, msg in enumerate(results):
        assert f"value_{i} = {i}" in msg["content"], msg["content"]
        assert "raised" not in msg["content"]
    assert all(entry[4].startswith("wilbur-read") for entry in log)
    assert not [t for t in threading.enumerate() if t.name.startswith("wilbur-read")]


def test_single_read_runs_inline_control(make_agent, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    a, _ = make_agent([Reply(content="", tool_calls=[call("read_file", path="a.py")]),
                       say("done")])
    log = _instrument(a, ["read_file"])
    a.run("read")
    assert log[0][4] == threading.current_thread().name


def test_write_in_a_batch_stays_sequential(make_agent, tmp_path):
    (tmp_path / "x.py").write_text("x = 1\n")
    (tmp_path / "y.py").write_text("y = 1\n")
    calls = [call("read_file", path="x.py"), call("read_file", path="y.py"),
             call("write_file", path="z.py", content="z = 42\n"),
             call("read_file", path="z.py")]
    a, _ = make_agent([Reply(content="", tool_calls=calls), say("done")])
    log = _instrument(a, ["read_file", "write_file"])
    a.run("go")
    order = [(e[1], e[2]) for e in log]
    write_at = order.index(("write_file", "z.py"))
    # Both earlier reads started before the write, the later read after it.
    assert set(order[:write_at]) == {("read_file", "x.py"), ("read_file", "y.py")}
    assert order[write_at + 1:] == [("read_file", "z.py")]
    write_entry = log[write_at]
    assert write_entry[3] == {"write_file": 1}, "a read overlapped the write"
    results = _tool_results(a)
    assert [m["tool_name"] for m in results] == ["read_file", "read_file",
                                                  "write_file", "read_file"]
    assert "z = 42" in results[3]["content"]
    assert "x = 1" in results[0]["content"] and "y = 1" in results[1]["content"]


def test_batched_read_error_is_reported_like_inline(make_agent, tmp_path):
    (tmp_path / "ok.py").write_text("ok = 1\n")
    calls = [call("read_file", path="missing.py"), call("read_file", path="ok.py")]
    a, _ = make_agent([Reply(content="", tool_calls=calls), say("done")])
    a.run("go")
    results = _tool_results(a)
    assert "ok = 1" in results[1]["content"]
    assert a.state.failures and "missing.py" in a.state.failures[-1].what


# --- glob relative path ------------------------------------------------------

@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


def test_glob_relative_path_is_under_project_root(ctx, tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "src").mkdir(parents=True)
    (elsewhere / "src" / "decoy.py").write_text("")
    monkeypatch.chdir(elsewhere)
    r = build_registry()["glob"].run({"pattern": "*.py", "path": "src"}, ctx)
    assert not r.is_error
    assert str(tmp_path / "src" / "m.py") in r.output
    assert "decoy" not in r.output


# --- outside-project hint: no basename false positive --------------------------

def test_outside_hint_does_not_name_unrelated_same_basename(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "config.py").write_text("")
    other = tmp_path / "other_project"
    other.mkdir()  # a REAL directory elsewhere: its config.py is not ours
    hint = hints.outside_path_hint(str(other / "config.py"), str(root), want_existing=True)
    assert "did you mean" not in hint
    assert f"outside the project root {root}" in hint


def test_outside_hint_still_suggests_plausible_targets(tmp_path):
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("")
    (root / "mod.py").write_text("")
    # same relative name at root ('/pkg/a.py' -> 'pkg/a.py')
    assert f"did you mean {root / 'pkg' / 'a.py'}" in hints.outside_path_hint(
        "/pkg/a.py", str(root), want_existing=True)
    # invented prefix that does not exist: basename fallback still applies
    assert f"did you mean {root / 'mod.py'}" in hints.outside_path_hint(
        "/nonexistent-dir-xyz/mod.py", str(root), want_existing=True)
