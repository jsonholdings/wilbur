"""E2 (2.71.0): crash recovery, proven with a real SIGKILL.

The real `wilbur` REPL runs in a subprocess on the fake backend with a
throwaway home (config, sessions, history, event log all under tmp_path; the
real ~/.local/share/wilbur is never touched). The scripted model calls
`run_bash` with a long sleep; once the event log shows that tool call started
and run.json reads `working`, the process is killed with SIGKILL -- no
`finally`, no atexit, nothing of Wilbur's own runs. Then the documented
recovery must hold:

- run.json survives and reads `dead`, with `original_state: working`;
- `wilbur status --check` exits 1 (a dead run is a failure, not "ok");
- the session saved mid-turn reloads, and `wilbur --resume` finishes a new
  turn on it and exits cleanly (which removes run.json);
- the event log ends cleanly: every line is whole JSON, the killed run's last
  event is the `tool_start` with no `tool_end`, and the resumed run logs under
  a new run id.

Controls prove the checks can fail: a run that exits normally leaves no
run.json and `status --check` exits 0, and a torn last line is detected by the
same whole-line check the crash case passes.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="needs SIGKILL and /proc")


def _env(home: Path) -> dict[str, str]:
    (home / "config.json").write_text(json.dumps({"event_log": True}))
    env = dict(os.environ,
               WILBUR_CONFIG=str(home / "config.json"),
               WILBUR_SESSIONS=str(home / "sessions"),
               WILBUR_HISTORY=str(home / "history"),
               WILBUR_USER_SKILLS_DIR=str(home / "skills"),
               WILBUR_GLOBAL_INSTRUCTIONS=str(home / "global-WILBUR.md"),
               WILBUR_LAST_PROJECT=str(home / "state" / "last-project"),
               WILBUR_GPU_HOLDERS_DIR=str(home / "gpu-holders"),
               WILBUR_EVENT_LOG=str(home / "events" / "events.jsonl"),
               WILBUR_GPU_PRIORITY="0", NO_COLOR="1")
    env.pop("WILBUR_RESTART_SESSION", None)
    return env


def _script(home: Path, name: str, turns: list) -> str:
    p = home / name
    p.write_text(json.dumps(turns))
    return str(p)


def _repl(home: Path, work: Path, script: str, *extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "wilbur", "-C", str(work), "--yes",
         "--url", "http://127.0.0.1:9", "--backend", "fake", "--fake-script", script, *extra],
        cwd=REPO, env=_env(home), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)


def _events(home: Path) -> list[dict]:
    p = home / "events" / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _whole_lines(path: Path) -> bool:
    """The check E2 relies on: the file ends in a newline and every line parses."""
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        return False
    try:
        for line in raw.decode().splitlines():
            json.loads(line)
    except ValueError:
        return False
    return True


def _run_files(home: Path) -> list[Path]:
    d = home / "sessions"
    return sorted(d.glob("*.run.json")) if d.exists() else []


def _status_check(home: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "wilbur", "status", "--check"],
                          cwd=REPO, env=_env(home), capture_output=True, text=True, timeout=60)


def _wait_for(predicate, timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _sleeping_tool_started(home: Path) -> bool:
    started = any(e["event"] == "tool_start" and e["data"].get("name") == "run_bash"
                  for e in _events(home))
    if not started:
        return False
    for p in _run_files(home):
        try:
            if json.loads(p.read_text()).get("state") == "working":
                return True
        except (OSError, ValueError):
            continue
    return False


def _finish(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=30)


def test_sigkill_mid_tool_call_recovers_as_documented(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    script = _script(home, "crash.json", [
        {"tool_calls": [{"name": "run_bash", "arguments": {"command": "sleep 60"}}]},
        "never reached",
    ])
    proc = _repl(home, work, script)
    try:
        proc.stdin.write("run the slow command\n")
        proc.stdin.flush()
        assert _wait_for(lambda: _sleeping_tool_started(home)), (
            f"tool call never started; events={[e['event'] for e in _events(home)]}")
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)
    finally:
        _finish(proc)
    assert proc.returncode == -signal.SIGKILL

    # 1. run.json survives the kill and reads dead.
    files = _run_files(home)
    assert len(files) == 1, files
    session_id = files[0].name[: -len(".run.json")]
    status_json = subprocess.run([sys.executable, "-m", "wilbur", "status", "--json"],
                                 cwd=REPO, env=_env(home), capture_output=True, text=True,
                                 timeout=60)
    rows = json.loads(status_json.stdout)
    assert [r["state"] for r in rows] == ["dead"]
    assert rows[0]["original_state"] == "working"
    assert rows[0]["session_id"] == session_id

    # 2. status --check fails on the dead run.
    assert _status_check(home).returncode == 1

    # 3. The event log ends cleanly at the killed tool call.
    log = home / "events" / "events.jsonl"
    assert _whole_lines(log)
    assert (log.stat().st_mode & 0o777) == 0o600
    events = _events(home)
    killed_run = events[-1]["run_id"]
    assert killed_run
    assert events[-1]["event"] == "tool_start"
    assert events[-1]["data"]["name"] == "run_bash"
    assert events[-1]["session_id"] == session_id
    assert not any(e["event"] == "tool_end" for e in events if e["run_id"] == killed_run)
    # The log is redacted: the command text is reduced to a length.
    assert "sleep 60" not in log.read_text()

    # 4. The session saved mid-turn reloads, with the interrupted tool call in it.
    saved = json.loads((home / "sessions" / f"{session_id}.json").read_text())
    calls = [c for m in saved["messages"] if m.get("role") == "assistant"
             for c in (m.get("tool_calls") or [])]
    assert any(c["function"]["name"] == "run_bash" for c in calls), calls

    # 5. --resume finishes a new turn on it and exits cleanly.
    resume_script = _script(home, "resume.json", ["Recovered after the crash."])
    proc2 = _repl(home, work, resume_script, "--resume", session_id)
    try:
        out, err = proc2.communicate("carry on\n", timeout=60)
    finally:
        _finish(proc2)
    assert proc2.returncode == 0, err
    assert "Recovered after the crash." in out
    assert _run_files(home) == []          # clean exit removes run.json
    assert _status_check(home).returncode == 0
    after = _events(home)
    assert _whole_lines(log)
    resumed_runs = {e["run_id"] for e in after[len(events):]}
    assert resumed_runs and killed_run not in resumed_runs
    assert all(e["session_id"] == session_id for e in after[len(events):])


def test_control_clean_exit_leaves_no_run_file_and_check_passes(tmp_path):
    """Control: without the kill, the same assertions come out the other way,
    so the crash test is measuring the kill, not a fixture artefact."""
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    script = _script(home, "ok.json", [
        {"tool_calls": [{"name": "run_bash", "arguments": {"command": "true"}}]},
        "All done.",
    ])
    proc = _repl(home, work, script)
    try:
        out, err = proc.communicate("run it\n", timeout=60)
    finally:
        _finish(proc)
    assert proc.returncode == 0, err
    assert _run_files(home) == []
    assert _status_check(home).returncode == 0
    events = _events(home)
    assert any(e["event"] == "tool_end" for e in events)
    assert events[-1]["event"] != "tool_start"


def test_control_torn_last_line_is_detected(tmp_path):
    """Control: the whole-line check that the crash case passes does fail on a
    torn write, and eventlog.read skips the torn line rather than raising."""
    from wilbur import eventlog
    log = tmp_path / "events.jsonl"
    good = json.dumps({"event": "tool_start", "run_id": "r", "data": {}}) + "\n"
    log.write_text(good)
    assert _whole_lines(log)
    log.write_text(good + '{"event": "tool_e')
    assert not _whole_lines(log)
    assert [r["event"] for r in eventlog.read(log)] == ["tool_start"]


def test_control_dead_pid_file_fails_status_check(tmp_path):
    """Control: a run.json whose pid is gone (and was never wilbur) makes
    `status --check` exit 1 on its own, so check 2 above can fail."""
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (home / "sessions" / "x.run.json").write_text(json.dumps(
        {"pid": dead.pid, "session_id": "x", "state": "working", "since": time.time(),
         "last_progress_at": time.time()}))
    assert _status_check(home).returncode == 1
    (home / "sessions" / "x.run.json").unlink()
    assert _status_check(home).returncode == 0
