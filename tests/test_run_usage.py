"""run-usage-and-subagents (Fleet-Wilbur handoff, 2026-10-16): run.json carries
`started_at` and cumulative `usage`; `<id>.subagents.json` mirrors the
subagent registry; `wilbur status --json` returns both. Fake backend only."""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.agent as agent_mod
from wilbur import runstate, status
from wilbur.config import Config
from wilbur.llm import Reply
from wilbur.repl import Repl
from wilbur.tools import agents_registry

from conftest import FakeClient


def _repl(monkeypatch, tmp_path, replies):
    monkeypatch.setattr(agent_mod, "OllamaClient", FakeClient(replies))
    r_fd, w_fd = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(r_fd))
    os.close(w_fd)
    repl = Repl(Config(), str(tmp_path))
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


def test_write_defaults_usage_and_started_at():
    runstate.write("u1", state=runstate.WORKING)
    data = runstate.read("u1")
    assert data["started_at"] is None
    assert data["usage"] == runstate.EMPTY_USAGE
    assert runstate.remove("u1")


def test_since_is_kept_when_given():
    runstate.write("u2", state=runstate.WORKING, since=123.0, started_at=100.0)
    data = runstate.read("u2")
    assert data["since"] == 123.0 and data["started_at"] == 100.0
    runstate.remove("u2")


def test_usage_from_stats_sums_tokens():
    u = runstate.usage_from_stats({"calls": 2, "in_tokens": 30, "out_tokens": 12, "seconds": 1.23456})
    assert u == {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42,
                 "context": None, "calls": 2, "seconds": 1.235}


def test_repl_run_json_carries_started_at_and_cumulative_usage(monkeypatch, tmp_path):
    replies = [Reply(content="Should a.txt be JSON?", tool_calls=[],
                     prompt_tokens=40, eval_tokens=7)]
    repl = _repl(monkeypatch, tmp_path, replies)
    seen = []
    real = runstate.write

    def spy(session_id, **kw):
        seen.append(kw)
        return real(session_id, **kw)
    monkeypatch.setattr(runstate, "write", spy)
    _drive(repl, ["make a.txt"])
    starts = {kw["started_at"] for kw in seen}
    assert len(starts) == 1 and None not in starts  # set once, on the first write
    last = seen[-1]
    assert last["state"] == "stopped"
    assert last["usage"]["prompt_tokens"] == 40
    assert last["usage"]["completion_tokens"] == 7
    assert last["usage"]["total_tokens"] == 47 and last["usage"]["calls"] == 1
    # the token refresh mid-turn keeps the working state's `since`
    working = [kw for kw in seen if kw["state"] == "working"]
    assert len(working) >= 2 and len({kw["since"] for kw in working}) == 1
    assert runstate.read(repl.session_id) is None


def test_registry_persists_every_transition_and_clear_unbinds():
    agents_registry.clear()
    writes = []
    agents_registry.bind_persist(writes.append)
    rec = agents_registry.register("look around", "explore")
    agents_registry.update(rec.id, current="tool: read_file")
    agents_registry.update(rec.id, current="")
    agents_registry.finish(rec.id, "done")
    last = writes[-1][0]
    assert last["state"] == "done" and last["ended"] is not None
    assert last["last_tool"] == "read_file" and last["label"] == "look around"
    assert len(writes) >= 5  # bind + register + 2 updates + finish
    agents_registry.clear()
    assert agents_registry._persist is None


def test_persist_hook_failure_never_fails_a_subagent():
    agents_registry.clear()

    def boom(rows):
        raise RuntimeError("disk full")
    agents_registry.bind_persist(boom)
    rec = agents_registry.register("x", "general")
    agents_registry.finish(rec.id)
    assert agents_registry.snapshot()[0].status == "done"
    agents_registry.clear()


def test_subagents_file_round_trip_and_removed_with_run_json():
    runstate.write("u3", state=runstate.WORKING)
    rows = [{"id": 1, "label": "a", "state": "running"}]
    p = runstate.write_subagents("u3", rows)
    assert p.name == "u3.subagents.json"
    assert json.loads(p.read_text())["subagents"] == rows
    assert runstate.read_subagents("u3") == rows
    assert not list(p.parent.glob("*.tmp"))
    runstate.remove("u3")
    assert not p.exists() and runstate.read_subagents("u3") == []


def test_read_subagents_tolerates_garbage():
    p = runstate.subagents_path("u4")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json")
    assert runstate.read_subagents("u4") == []
    p.write_text(json.dumps({"subagents": "nope"}))
    assert runstate.read_subagents("u4") == []
    p.unlink()


def test_repl_run_binds_and_unbinds_the_persist_hook(monkeypatch, tmp_path):
    agents_registry.clear()
    repl = _repl(monkeypatch, tmp_path, [Reply(content="Which format?", tool_calls=[])])
    seen = []
    real = runstate.write_subagents

    def spy(session_id, rows):
        seen.append(session_id)
        return real(session_id, rows)
    monkeypatch.setattr(runstate, "write_subagents", spy)
    _drive(repl, ["hi"])
    assert seen and set(seen) == {repl.session_id}
    assert agents_registry._persist is None
    assert not runstate.subagents_path(repl.session_id).exists()


def test_status_json_includes_usage_started_at_and_subagents(capsys):
    runstate.write("u5", state=runstate.WORKING, started_at=50.0,
                   usage=runstate.usage_from_stats({"in_tokens": 3, "out_tokens": 2}))
    runstate.write_subagents("u5", [{"id": 1, "label": "s", "state": "running"}])
    rc = status.status_main(["--json"])
    rows = json.loads(capsys.readouterr().out)
    row = next(r for r in rows if r["session_id"] == "u5")
    assert rc == 0
    assert row["started_at"] == 50.0 and row["usage"]["total_tokens"] == 5
    assert row["subagents"][0]["label"] == "s"
    runstate.remove("u5")


def test_control_status_json_without_subagents_file_is_empty_list(capsys):
    runstate.write("u6", state=runstate.WORKING)
    status.status_main(["--json"])
    row = next(r for r in json.loads(capsys.readouterr().out) if r["session_id"] == "u6")
    assert row["subagents"] == []
    runstate.remove("u6")


def test_parallel_tools_switch_is_gone():
    assert not hasattr(Config(), "parallel_tools")


def test_surface_defaults_to_cli(monkeypatch):
    monkeypatch.delenv("WILBUR_SURFACE", raising=False)
    runstate.write("s1", state=runstate.WORKING)
    assert runstate.read("s1")["surface"] == "cli"


def test_surface_vscode_when_extension_env_set(monkeypatch):
    monkeypatch.setenv("WILBUR_SURFACE", "vscode")
    runstate.write("s2", state=runstate.WORKING)
    assert runstate.read("s2")["surface"] == "vscode"


def test_surface_unknown_value_is_cli(monkeypatch):
    monkeypatch.setenv("WILBUR_SURFACE", "emacs")
    runstate.write("s3", state=runstate.WORKING)
    assert runstate.read("s3")["surface"] == "cli"
