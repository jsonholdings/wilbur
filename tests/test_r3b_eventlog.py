"""R3b and E3a/E3b (2.70.0).

R3b (the bench-row counts are in test_bench_runner.py, not mirrored): a
first-turn batching hint (`batch_hint_first_turn`) and a todo_write pairing
hint (`todo_write_pair_hint`), both off by default until M2 measures them.
E3a: wilbur.eventlog writes 0600 JSONL with the schema, rotates, and keeps no
model text or secrets. E3b: the Agent tees its events into it (display
unchanged) and the REPL's keep-going stop is logged with its reason.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import call, say
from wilbur import eventlog
from wilbur.agent import BATCH_HINT, TODO_PAIR_HINT
from wilbur.llm import Reply


def _todo_round() -> Reply:
    return Reply(content="", tool_calls=[call("todo_write", todos=[
        {"content": "step one", "status": "in_progress"}])])


# ---------------- R3b ----------------

def test_first_turn_hint_appended_once_after_first_user_turn(make_agent, tmp_path):
    a, _ = make_agent([say("one"), say("two")], batch_hint_first_turn=True)
    assert a.run("first") == "one"
    a.run("second")
    hints = [i for i, m in enumerate(a.messages) if m.get("content") == BATCH_HINT]
    assert len(hints) == 1
    assert a.messages[hints[0] - 1]["content"] == "first"   # right after turn 1
    assert a.messages[0]["role"] == "system"                # prefix untouched


def test_first_turn_hint_off_by_default_control(make_agent):
    a, _ = make_agent([say("one")])
    a.run("first")
    assert not any(m.get("content") == BATCH_HINT for m in a.messages)


def test_todo_only_round_gets_pairing_hint(make_agent):
    a, _ = make_agent([_todo_round(), say("done")], todo_write_pair_hint=True)
    a.run("plan it")
    tool_msgs = [m for m in a.messages if m.get("role") == "tool"]
    assert tool_msgs and TODO_PAIR_HINT in tool_msgs[-1]["content"]


def test_todo_paired_with_action_gets_no_hint(make_agent, tmp_path):
    (tmp_path / "x.py").write_text("x = 1\n")
    paired = Reply(content="", tool_calls=[
        call("todo_write", todos=[{"content": "s", "status": "in_progress"}]),
        call("read_file", path="x.py")])
    a, _ = make_agent([paired, say("done")], todo_write_pair_hint=True)
    a.run("plan it")
    assert not any(TODO_PAIR_HINT in str(m.get("content")) for m in a.messages)


def test_todo_hint_off_by_default_control(make_agent):
    a, _ = make_agent([_todo_round(), say("done")])
    a.run("plan it")
    assert not any(TODO_PAIR_HINT in str(m.get("content")) for m in a.messages)


# ---------------- E3a ----------------

def test_eventlog_schema_mode_and_scrub(tmp_path, monkeypatch):
    fake = "hunter2" * 2            # built at runtime: no literal for the mirror scrub
    keyfile = tmp_path / "k.env"
    keyfile.write_text("k=" + fake + "\n")
    monkeypatch.setenv("WILBUR_SECRET_FILES", str(keyfile))
    log = eventlog.EventLog(tmp_path / "ev" / "events.jsonl", clock=lambda: 5.0)
    assert log.write("tool_end", run_id="r1", session_id="s1", round_no=2,
                     data={"name": "run_bash " + fake, "error": False,
                           "output": "secret file body", "args": {"cmd": "cat x"},
                           "seconds": 0.5})
    rows = eventlog.read(log.path)
    assert rows == [{"ts": 5.0, "run_id": "r1", "session_id": "s1", "event": "tool_end",
                     "round": 2, "data": {"name": "run_bash <REDACTED>", "error": False,
                                          "output_chars": 16, "args_count": 1,
                                          "seconds": 0.5}}]
    assert stat.S_IMODE(os.stat(log.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(log.path.parent).st_mode) == 0o700
    raw = log.path.read_text()
    assert "hunter2" not in raw and "secret file body" not in raw and "cat x" not in raw


def test_eventlog_rotates_and_skips_display_events(tmp_path):
    log = eventlog.EventLog(tmp_path / "e.jsonl", max_bytes=1024, keep=2)
    assert not log.write("assistant_delta", data={"text": "x"})
    for i in range(60):
        log.write("tool_start", data={"name": "read_file", "i": i})
    assert (tmp_path / "e.jsonl.1").exists() and (tmp_path / "e.jsonl.2").exists()
    assert not (tmp_path / "e.jsonl.3").exists()
    assert os.path.getsize(tmp_path / "e.jsonl") <= 1024


def test_eventlog_read_skips_torn_last_line(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_text(json.dumps({"event": "a"}) + "\n" + '{"event": "b", "da')
    assert [r["event"] for r in eventlog.read(p)] == ["a"]


# ---------------- E3b ----------------

def test_agent_tees_events_to_log_without_changing_display(make_agent, tmp_path, monkeypatch):
    target = tmp_path / "agent-events.jsonl"
    monkeypatch.setenv("WILBUR_EVENT_LOG", str(target))
    (tmp_path / "f.py").write_text("v = 1\n")
    a, _ = make_agent([Reply(content="", tool_calls=[call("read_file", path="f.py")]),
                       say("done")])
    a.session_id = "sess-1"
    shown: list[str] = []
    a._display_event = lambda kind, data: shown.append(kind)
    assert a.run("go") == "done"
    rows = eventlog.read(target)
    kinds = [r["event"] for r in rows]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert "tool_start" in kinds and "tool_end" in kinds and "assistant" in kinds
    assert {r["session_id"] for r in rows} == {"sess-1"}
    assert len({r["run_id"] for r in rows}) == 1
    assert max(r["round"] for r in rows) == 2
    assert "run_start" not in shown and "assistant" in shown   # display unchanged
    assert "v = 1" not in target.read_text()                   # no file contents


def test_agent_event_log_off_writes_nothing_control(make_agent, tmp_path, monkeypatch):
    target = tmp_path / "off.jsonl"
    monkeypatch.setenv("WILBUR_EVENT_LOG", str(target))
    a, _ = make_agent([say("done")], event_log=False)
    a.run("go")
    assert not target.exists()


def test_compact_and_parse_retry_events_are_logged_kinds(make_agent, tmp_path, monkeypatch):
    target = tmp_path / "k.jsonl"
    monkeypatch.setenv("WILBUR_EVENT_LOG", str(target))
    a, _ = make_agent([say("done")])
    a.on_event("compact", {"before": 10, "after": 4, "reason": "budget"})
    a.on_event("tool_parse_retry", {"attempt": 1})
    rows = eventlog.read(target)
    assert [r["event"] for r in rows] == ["compact", "tool_parse_retry"]
    assert rows[0]["data"]["reason"] == "budget"


def test_repl_stop_is_logged_with_reason():
    src = (Path(__file__).resolve().parents[1] / "wilbur" / "repl.py").read_text()
    body = src[src.index("def stop(reason: str, text: str)"):][:500]
    assert 'log("stop", {"reason": str(reason)})' in body


def test_log_faults_never_break_the_agent(make_agent, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("log broke")
    monkeypatch.setattr(eventlog.EventLog, "__init__", boom)
    a, _ = make_agent([say("done")])          # construction survives
    assert a.event_log is None and a.run("go") == "done"


def test_log_write_error_never_reaches_the_run(make_agent, monkeypatch):
    a, _ = make_agent([say("done")])
    def boom(*_a, **_k):
        raise ValueError("write broke")
    monkeypatch.setattr(a.event_log, "write", boom)
    assert a.run("go") == "done"
