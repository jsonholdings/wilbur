"""BACKLOG tool-background-jobs: job_start/job_status/job_output/job_stop."""
import os
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools import build_registry
from wilbur.tools.base import Approval, ToolContext

JOB_TOOLS = {"job_start", "job_status", "job_output", "job_stop"}


@pytest.fixture
def env(tmp_path):
    """A registry whose job logs go to a temp dir (never Wilbur's runtime data);
    every job left running is stopped by exact ID in teardown."""
    config = Config()
    config.jobs_dir = str(tmp_path / "jobs")
    config.jobs_max_running = 2
    approvals = []

    def approve(tool, detail):
        approvals.append((tool, detail))
        return Approval.GRANTED
    work = tmp_path / "work"
    work.mkdir()
    ctx = ToolContext(cwd=str(work), config=config, read_files={}, approve=approve)
    registry = build_registry(config=config)
    manager = registry["job_start"].manager
    yield registry, ctx, approvals, manager
    for job in list(manager.jobs.values()):
        manager.stop(job, "teardown")


def _start(registry, ctx, command, **extra):
    return registry["job_start"].run({"command": command, **extra}, ctx)


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_start_returns_at_once_and_status_output_tail_while_running(env):
    registry, ctx, approvals, manager = env
    began = time.monotonic()
    out = _start(registry, ctx, "for i in 1 2 3 4 5; do echo line$i; done; sleep 30",
                 name="slow build")
    assert time.monotonic() - began < 5, "job_start must not wait for the command"
    assert not out.is_error and "job-1" in out.output
    assert approvals[-1][0] == "run_bash"
    job = manager.jobs["job-1"]
    assert job.log.parent.parent == Path(ctx.config.jobs_dir)
    status = registry["job_status"].run({"id": "job-1"}, ctx)
    assert "running" in status.output and "slow build" in status.output
    assert _wait_for(lambda: "line5" in job.log.read_text())
    tail = registry["job_output"].run({"id": "job-1", "tail": 2}, ctx)
    assert "line4" in tail.output and "line5" in tail.output and "line3" not in tail.output
    assert "running" in tail.output
    listing = registry["job_status"].run({}, ctx)
    assert "job-1" in listing.output


def test_finished_job_reports_exit_code(env):
    registry, ctx, _, manager = env
    _start(registry, ctx, "echo done; exit 3")
    assert _wait_for(lambda: manager.jobs["job-1"].proc.poll() is not None)
    assert "exited 3" in registry["job_status"].run({"id": "job-1"}, ctx).output
    assert "done" in registry["job_output"].run({"id": "job-1"}, ctx).output


def test_stop_by_exact_id_only(env):
    registry, ctx, _, manager = env
    _start(registry, ctx, "sleep 30", name="sleeper")
    _start(registry, ctx, "sleep 30", name="sleeper")
    first, second = manager.jobs["job-1"], manager.jobs["job-2"]
    for bad in ("sleeper", "job-*", "job", "job-1 ", "1", "job-1|job-2", "sleep", None, 1):
        out = registry["job_stop"].run({"id": bad}, ctx)
        assert out.is_error and "Nothing was changed" in out.output, bad
    assert first.proc.poll() is None and second.proc.poll() is None
    out = registry["job_stop"].run({"id": "job-9"}, ctx)
    assert out.is_error
    out = registry["job_stop"].run({"id": "job-1"}, ctx)
    assert not out.is_error and "Stopped job-1" in out.output
    assert first.proc.poll() is not None and not _alive(first.proc.pid)
    assert second.proc.poll() is None, "stopping job-1 must not touch job-2"
    assert "stopped (job_stop)" in registry["job_status"].run({"id": "job-1"}, ctx).output
    again = registry["job_stop"].run({"id": "job-1"}, ctx)
    assert not again.is_error and "nothing to stop" in again.output


def test_stop_kills_the_whole_process_group(env, tmp_path):
    registry, ctx, _, manager = env
    pidfile = tmp_path / "child.pid"
    _start(registry, ctx, f"sleep 30 & echo $! > {pidfile}; wait")
    assert _wait_for(lambda: pidfile.exists() and pidfile.read_text().strip())
    child = int(pidfile.read_text())
    registry["job_stop"].run({"id": "job-1"}, ctx)
    assert _wait_for(lambda: not _alive(child))


def test_cap_on_running_jobs(env):
    registry, ctx, _, manager = env
    assert not _start(registry, ctx, "sleep 30").is_error
    assert not _start(registry, ctx, "sleep 30").is_error
    out = _start(registry, ctx, "sleep 30")
    assert out.is_error and "cap 2" in out.output
    assert len(manager.jobs) == 2
    registry["job_stop"].run({"id": "job-1"}, ctx)
    assert not _start(registry, ctx, "echo ok").is_error, "a freed slot can be reused"


@pytest.fixture
def never_execute(monkeypatch):
    """Refused commands must never reach a process: if a guard misses one, the
    test fails here instead of running it (2026-11-27: an earlier draft of this
    test ran `pkill -f build` and `killall python3` for real)."""
    from wilbur.tools import contained

    def boom(*args, **kwargs):
        raise AssertionError(f"a refused command reached execution: {args!r}")
    monkeypatch.setattr(contained, "popen", boom)
    monkeypatch.setattr(contained, "shell", boom)


@pytest.mark.parametrize("command", [
    "sudo make install",
    "kill -9 1",
    "kill -- -1",
    "pkill -u desktopuser1 sleep",
    "git add -A",
    "git reset --hard",
    "git stash",
])
def test_guards_apply_like_run_bash(env, never_execute, command):
    registry, ctx, approvals, manager = env
    out = _start(registry, ctx, command)
    bash = registry["run_bash"].run({"command": command}, ctx)
    assert out.is_error and bash.is_error
    assert out.output == bash.output, "job_start refuses with run_bash's own message"
    assert not manager.jobs and not approvals, "refused before approval, nothing started"


def test_deny_list_applies(env, never_execute):
    registry, ctx, approvals, manager = env
    ctx.config.denied_bash = ["rm -rf /"]
    assert _start(registry, ctx, "rm -rf / --no-preserve-root").is_error
    assert not manager.jobs


def test_denied_approval_starts_nothing(env, never_execute):
    registry, ctx, _, manager = env
    ctx.approve = lambda tool, detail: Approval.DENIED
    out = _start(registry, ctx, "echo hi")
    assert out.is_error and "did not run" in out.output
    assert not manager.jobs


def test_session_end_stops_jobs_unless_keep_running(env):
    registry, ctx, _, manager = env
    _start(registry, ctx, "sleep 30", name="ephemeral")
    out = _start(registry, ctx, "sleep 30", name="survivor", keep_running=True)
    assert "keep_running" in out.output
    ephemeral, survivor = manager.jobs["job-1"], manager.jobs["job-2"]
    assert manager.end_session() == ["job-1"]
    assert ephemeral.proc.poll() is not None
    assert survivor.proc.poll() is None, "keep_running survives the session end"
    assert "session ended" in ephemeral.stopped_by


def test_session_end_runs_at_process_exit(tmp_path):
    """The atexit hook really stops a non-keep job when the process ends."""
    pidfile = tmp_path / "pid"
    script = f"""
import sys, time
sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
from wilbur.config import Config
from wilbur.tools import build_registry
from wilbur.tools.base import Approval, ToolContext
config = Config(); config.jobs_dir = {str(tmp_path / 'jobs')!r}
ctx = ToolContext(cwd={str(tmp_path)!r}, config=config, read_files={{}},
                  approve=lambda t, d: Approval.GRANTED)
reg = build_registry(config=config)
reg["job_start"].run({{"command": "sleep 60"}}, ctx)
open({str(pidfile)!r}, "w").write(str(reg["job_start"].manager.jobs["job-1"].proc.pid))
"""
    subprocess.run([sys.executable, "-c", script], check=True, timeout=60)
    pid = int(pidfile.read_text())
    assert _wait_for(lambda: not _alive(pid)), "job outlived the session"


def test_switch_off_removes_the_tools():
    on = build_registry(config=Config())
    assert JOB_TOOLS <= set(on)
    config = Config()
    config.background_jobs = False
    off = build_registry(config=config)
    assert not (JOB_TOOLS & set(off))
    assert "run_bash" in off


def test_job_logs_are_pruned(tmp_path):
    """Old session dirs go only when outside the newest N AND past the age."""
    from wilbur.tools.jobs import prune_job_logs
    root = tmp_path / "jobs"
    old_stamp = time.time() - 30 * 86400
    names = [f"202611{d:02d}-120000-100-abc" for d in range(1, 6)]
    for name in names:
        d = root / name
        d.mkdir(parents=True)
        (d / "job-1-x.log").write_text("out\n")
        os.utime(d, (old_stamp, old_stamp))
    fresh = root / "20261120-120000-100-abc"
    fresh.mkdir()
    (fresh / "job-1-x.log").write_text("out\n")
    other = root / "not-a-session"
    other.mkdir()
    os.utime(other, (old_stamp, old_stamp))
    keeper = root / "20261101-000000-1-def"   # old, but holds a non-log file
    keeper.mkdir()
    (keeper / "notes.txt").write_text("keep")
    os.utime(keeper, (old_stamp, old_stamp))

    removed = prune_job_logs(root, keep=3, max_age_days=7)
    left = {p.name for p in root.iterdir()}
    assert fresh.name in left and other.name in left and keeper.name in left
    assert names[-1] in left and names[-2] in left        # newest kept
    assert {p.name for p in removed} == set(names[:3])
    # Control: a young dir outside the newest N is kept.
    young = root / "20261102-000000-1-abc"
    young.mkdir()
    assert young not in prune_job_logs(root, keep=1, max_age_days=7)
    assert young.exists()


def test_job_start_prunes_on_new_session(tmp_path):
    root = tmp_path / "jobs"
    stale = root / "20260101-000000-1-abc"
    stale.mkdir(parents=True)
    (stale / "job-1-x.log").write_text("x")
    os.utime(stale, (0, 0))
    config = Config()
    config.jobs_dir = str(root)
    config.jobs_keep_sessions = 1
    ctx = ToolContext(cwd=str(tmp_path), config=config, read_files={},
                      approve=lambda t, d: Approval.GRANTED)
    registry = build_registry(config=config)
    assert not registry["job_start"].run({"command": "true"}, ctx).is_error
    manager = registry["job_start"].manager
    assert _wait_for(lambda: not manager.running())
    assert not stale.exists()
