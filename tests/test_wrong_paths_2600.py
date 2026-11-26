"""2.60.0: wrong absolute paths (trace-2590: 26 failed read_file/list_files
where the model dropped or mangled the hidden `.bench-work-<task>-N` root
segment) and the 2.59.0 read-batch verifier minors (a breaker-blocked read
ran on the pool; queued reads still ran after cancel)."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import call, say
from wilbur import agent as agent_mod
from wilbur.agent import SYSTEM_PROMPT
from wilbur.config import Config
from wilbur.llm import Reply
from wilbur.tools import build_registry, hints
from wilbur.tools.base import Approval, ToolContext


def _bench_root(tmp_path):
    parent = tmp_path / "wilbur-bench-abc"
    root = parent / ".bench-work-slugify-1"
    (root / "src").mkdir(parents=True)
    (root / "slugify.py").write_text("def slugify(s): return s\n")
    (root / "src" / "util.py").write_text("X = 1\n")
    return parent, root


def _ctx(root):
    return ToolContext(cwd=str(root), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


# --- (1) project-relative paths ----------------------------------------------

def test_static_prompt_says_use_project_relative_paths():
    assert "Use paths relative to the project root" in SYSTEM_PROMPT


def test_relative_path_reads_from_project_root(tmp_path):
    _, root = _bench_root(tmp_path)
    result = build_registry()["read_file"].run({"path": "src/util.py"}, _ctx(root))
    assert not result.is_error and "X = 1" in result.output


def test_dropped_hidden_segment_hint_names_project_path(tmp_path):
    parent, root = _bench_root(tmp_path)
    result = build_registry()["read_file"].run(
        {"path": str(parent / "slugify.py")}, _ctx(root))
    assert result.is_error
    assert "relative: 'slugify.py'" in result.output, result.output


def test_dropped_segment_nested_tail(tmp_path):
    parent, root = _bench_root(tmp_path)
    hint = hints.outside_path_hint(str(parent / "src" / "util.py"), str(root),
                                   want_existing=True)
    assert "relative: 'src/util.py'" in hint, hint


def test_mangled_hidden_segment_hint(tmp_path):
    parent, root = _bench_root(tmp_path)
    raw = str(parent / ".bench-work-slugfy-1" / "slugify.py")
    hint = hints.outside_path_hint(raw, str(root), want_existing=True)
    assert "relative: 'slugify.py'" in hint, hint


def test_list_files_missing_dir_hint_names_project_dir(tmp_path):
    parent, root = _bench_root(tmp_path)
    result = build_registry()["list_files"].run({"path": str(parent / "src")}, _ctx(root))
    assert result.is_error and "did you mean 'src'" in result.output, result.output


def test_unrelated_real_dir_still_gets_no_guess_control(tmp_path):
    _, root = _bench_root(tmp_path)
    other = tmp_path / "other-project"
    other.mkdir()
    (other / "slugify.py").write_text("unrelated\n")
    hint = hints.outside_path_hint(str(other / "slugify.py"), str(root), want_existing=True)
    assert "did you mean" not in hint


# --- (2) read-batch verifier minors ------------------------------------------

class _Recorder:
    def __init__(self, tool, ran, on_run=None):
        self._tool, self._ran, self._on_run = tool, ran, on_run
        self.name, self.schema, self.description = tool.name, tool.schema, tool.description
        self.read_only = getattr(tool, "read_only", False)

    def run(self, args, ctx):
        self._ran.append(args.get("path"))
        if self._on_run:
            self._on_run()
        return self._tool.run(args, ctx)


def test_breaker_blocked_read_is_never_submitted(make_agent, tmp_path):
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text(f"{name}\n")
    calls = [call("read_file", path="a.py"), call("read_file", path="b.py"),
             call("read_file", path="c.py")]
    a, _ = make_agent([Reply(content="", tool_calls=calls), say("done")])
    blocked = f"read_file({a._arg_signature(calls[1])})"
    for _ in range(3):
        a.state.record_failure(blocked, "boom")
    ran: list = []
    a.registry["read_file"] = _Recorder(a.registry["read_file"], ran)
    a.run("go")
    assert "b.py" not in ran, ran
    assert sorted(ran) == ["a.py", "c.py"]  # control: unblocked reads ran
    tool_msgs = [m["content"] for m in a.messages if m.get("role") == "tool"]
    assert "blocked" in tool_msgs[1]


def test_cancel_stops_queued_batch_reads(make_agent, tmp_path, monkeypatch):
    for i in range(4):
        (tmp_path / f"f{i}.py").write_text(f"v{i}\n")
    calls = [call("read_file", path=f"f{i}.py") for i in range(4)]
    a, _ = make_agent([Reply(content="", tool_calls=calls), say("done")])
    monkeypatch.setattr(agent_mod, "READ_BATCH_WORKERS", 1)
    ran: list = []
    a.registry["read_file"] = _Recorder(a.registry["read_file"], ran,
                                        on_run=a.cancel_event.set)
    assert a.run("go") == "Cancelled by user."
    assert ran == ["f0.py"], ran
    assert not [t for t in threading.enumerate() if t.name.startswith("wilbur-read")]


def test_batch_without_cancel_runs_all_control(make_agent, tmp_path, monkeypatch):
    for i in range(4):
        (tmp_path / f"f{i}.py").write_text(f"v{i}\n")
    calls = [call("read_file", path=f"f{i}.py") for i in range(4)]
    a, _ = make_agent([Reply(content="", tool_calls=calls), say("done")])
    monkeypatch.setattr(agent_mod, "READ_BATCH_WORKERS", 1)
    ran: list = []
    a.registry["read_file"] = _Recorder(a.registry["read_file"], ran)
    assert a.run("go") == "done"
    assert ran == [f"f{i}.py" for i in range(4)]
