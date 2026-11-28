"""Background jobs (BACKLOG tool-background-jobs).

job_start runs a shell command detached, under the same checks as run_bash
(elevation, killguard, gitguard, default-branch push key, deny-list) and the
same contained-mode memory cap, with its output in a log file. job_status,
job_output and job_stop address a job by its exact ID (`job-<n>`) only:
there is no name or pattern match, so a stop can never reach a process this
session did not start.

Jobs end with the session (an atexit hook, or JobManager.end_session())
unless started with keep_running. At most `jobs_max_running` run at once.
"""
from __future__ import annotations

import atexit
import os
import re
import subprocess
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import contained, killguard
from .base import Approval, ToolContext, ToolResult, clamp, denial_message
from .shell import command_preflight

JOB_ID = re.compile(r"^job-[0-9]+$")
_NAME_OK = re.compile(r"[^A-Za-z0-9._-]+")


def default_jobs_dir() -> Path:
    env = os.environ.get("WILBUR_JOBS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".local" / "state" / "wilbur" / "jobs"


_SESSION_DIR = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9]+-[0-9a-f]+$")


def prune_job_logs(root: Path, keep: int, max_age_days: float,
                   current: Path | None = None) -> list[Path]:
    """Remove old job-log session directories under root; return those removed.

    A directory goes only when it is BOTH outside the newest `keep` and older
    than `max_age_days`, so a recent keep_running job's log survives. Only
    directories named like a session dir are touched, and only their `*.log`
    files are unlinked, one by one by exact path; a directory holding anything
    else is left in place (rmdir fails)."""
    try:
        dirs = [d for d in root.iterdir() if d.is_dir() and not d.is_symlink()
                and _SESSION_DIR.match(d.name) and d != current]
    except OSError:
        return []
    dirs.sort(key=lambda d: d.name, reverse=True)
    cutoff = time.time() - max_age_days * 86400
    removed = []
    # The current session dir (excluded above) counts as one of the newest.
    for old in dirs[max(keep - (current is not None), 0):]:
        try:
            if old.stat().st_mtime > cutoff:
                continue
            for log in old.iterdir():
                if log.suffix == ".log" and log.is_file() and not log.is_symlink():
                    log.unlink()
            old.rmdir()
            removed.append(old)
        except OSError:
            continue
    return removed


@dataclass
class Job:
    id: str
    name: str
    command: str
    log: Path
    proc: subprocess.Popen
    unit: str | None
    keep_running: bool
    started: float = field(default_factory=time.time)
    stopped_by: str = ""

    def state(self) -> str:
        code = self.proc.poll()
        if code is None:
            return "running"
        if self.stopped_by:
            return f"stopped ({self.stopped_by})"
        return f"exited {code}"

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "state": self.state(),
                "keep_running": self.keep_running, "pid": self.proc.pid,
                "elapsed_s": round(time.time() - self.started, 1),
                "command": self.command, "log": str(self.log)}


class JobManager:
    """Jobs this session started. One per registry, so one per agent."""

    def __init__(self, config: Any = None) -> None:
        self.config = config
        self.jobs: dict[str, Job] = {}
        self._counter = 0
        self._session_dir: Path | None = None
        # weakref: the hook must not keep a finished manager alive.
        ref = weakref.ref(self)
        atexit.register(lambda: (m := ref()) is not None and m.end_session())

    @property
    def max_running(self) -> int:
        return int(getattr(self.config, "jobs_max_running", 4))

    def _dir(self) -> Path:
        if self._session_dir is None:
            root = Path(getattr(self.config, "jobs_dir", "") or default_jobs_dir())
            self._session_dir = root / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{id(self):x}"
            self._session_dir.mkdir(parents=True, exist_ok=True)
            prune_job_logs(root, keep=int(getattr(self.config, "jobs_keep_sessions", 10)),
                           max_age_days=float(getattr(self.config, "jobs_log_max_age_days", 7)),
                           current=self._session_dir)
        return self._session_dir

    def running(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.proc.poll() is None]

    def start(self, command: str, name: str, cwd: str, keep_running: bool,
              memory_max: str) -> Job:
        self._counter += 1
        job_id = f"job-{self._counter}"
        safe = _NAME_OK.sub("-", name).strip("-")[:40] or "job"
        log = self._dir() / f"{job_id}-{safe}.log"
        with open(log, "wb") as handle:
            # Output goes to a file, never a pipe: a keep-running job must
            # not die of SIGPIPE when this process exits.
            proc, unit = contained.popen(["/bin/sh", "-c", command], cwd, memory_max,
                                         stdout=handle, stderr=subprocess.STDOUT)
        job = Job(job_id, name or safe, command, log, proc, unit, keep_running)
        # killguard: `kill <pid>` / `systemctl --user stop <unit>` of this job
        # counts as a process the session started.
        killguard.register_session_pid(proc.pid)
        killguard.register_session_unit(unit)
        self.jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        if not isinstance(job_id, str) or not JOB_ID.match(job_id):
            return None
        return self.jobs.get(job_id)

    def stop(self, job: Job, why: str) -> None:
        if job.proc.poll() is None:
            job.stopped_by = why
            # Exact targets only: this job's own scope unit and process group.
            contained.stop(job.proc, job.unit)

    def end_session(self) -> list[str]:
        """Stop every running job not marked keep_running; return their IDs."""
        stopped = []
        for job in list(self.jobs.values()):
            if job.keep_running or job.proc.poll() is not None:
                continue
            self.stop(job, "session ended")
            stopped.append(job.id)
        return stopped


def _tail(path: Path, lines: int, max_bytes: int = 256_000) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError as exc:
        return f"(log unreadable: {exc})"
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def _unknown(job_id: Any, manager: JobManager) -> ToolResult:
    known = ", ".join(manager.jobs) or "none"
    return ToolResult(f"No job with the exact ID {job_id!r} (IDs look like job-1; "
                      f"this session's jobs: {known}). Nothing was changed.", is_error=True)


class JobStart:
    name = "job_start"
    read_only = False
    description = ("Start a long shell command (build, test run) in the background and "
                   "return its job ID at once. Same rules as run_bash. It is stopped when "
                   "the session ends unless keep_running is true.")
    schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run"},
            "name": {"type": "string", "description": "Short name for the job"},
            "keep_running": {"type": "boolean",
                             "description": "Keep it running after the session ends"},
        },
        "required": ["command"],
    }

    def __init__(self, manager: JobManager) -> None:
        self.manager = manager

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult("No command given.", is_error=True)
        blocked, approved = command_preflight(command, ctx)
        if blocked is not None:
            return blocked
        running = self.manager.running()
        if len(running) >= self.manager.max_running:
            ids = ", ".join(j.id for j in running)
            return ToolResult(f"Refused: {len(running)} jobs already running (cap "
                              f"{self.manager.max_running}: {ids}). Wait for one or stop it "
                              f"with job_stop.", is_error=True)
        name = str(args.get("name") or "").strip()
        keep = bool(args.get("keep_running", False))
        label = f"background job{' (keeps running after the session)' if keep else ''}: {name or command[:60]}"
        outcome = Approval.GRANTED if approved else ctx.approve("run_bash", f"{label}\n    $ {command}")
        if outcome is not Approval.GRANTED:
            return ToolResult(denial_message("background job", outcome), is_error=True)
        memory_max = getattr(ctx.config, "tool_memory_max", contained.DEFAULT_MEMORY_MAX)
        try:
            job = self.manager.start(command, name, str(ctx.cwd), keep, memory_max)
        except OSError as exc:
            return ToolResult(f"Could not start the job: {exc}", is_error=True)
        return ToolResult(f"Started {job.id} ({job.name}), pid {job.proc.pid}"
                          f"{', keep_running' if keep else ''}. Check it with "
                          f"job_status / job_output(id={job.id!r}).")


class JobStatus:
    name = "job_status"
    read_only = True
    description = "State of one background job by exact ID, or of every job started so far."
    schema = {"type": "object",
              "properties": {"id": {"type": "string", "description": "Exact job ID, e.g. job-1"}}}

    def __init__(self, manager: JobManager) -> None:
        self.manager = manager

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        job_id = args.get("id")
        if job_id:
            job = self.manager.get(job_id)
            if job is None:
                return _unknown(job_id, self.manager)
            jobs = [job]
        else:
            jobs = list(self.manager.jobs.values())
        if not jobs:
            return ToolResult("No background jobs in this session.")
        lines = [f"{j.id} {j.name}: {j.state()}, {round(time.time() - j.started)}s"
                 f"{', keep_running' if j.keep_running else ''}\n    $ {j.command}" for j in jobs]
        return ToolResult("\n".join(lines))


class JobOutput:
    name = "job_output"
    read_only = True
    description = "The last lines of a background job's output, by exact ID."
    schema = {"type": "object",
              "properties": {"id": {"type": "string", "description": "Exact job ID"},
                             "tail": {"type": "integer", "description": "Lines, default 40"}},
              "required": ["id"]}

    def __init__(self, manager: JobManager) -> None:
        self.manager = manager

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        job = self.manager.get(args.get("id"))
        if job is None:
            return _unknown(args.get("id"), self.manager)
        try:
            lines = max(1, min(int(args.get("tail", 40)), 500))
        except (TypeError, ValueError):
            lines = 40
        body = _tail(job.log, lines) or "(no output yet)"
        body = clamp(body, ctx.config.max_tool_output_chars, ctx.config.max_tool_output_lines)
        return ToolResult(f"{job.id} {job.state()}\n{body}")


class JobStop:
    name = "job_stop"
    read_only = False
    description = "Stop one background job by its exact ID (no names or patterns)."
    schema = {"type": "object",
              "properties": {"id": {"type": "string", "description": "Exact job ID, e.g. job-1"}},
              "required": ["id"]}

    def __init__(self, manager: JobManager) -> None:
        self.manager = manager

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        job = self.manager.get(args.get("id"))
        if job is None:
            return _unknown(args.get("id"), self.manager)
        if job.proc.poll() is not None:
            return ToolResult(f"{job.id} already {job.state()}; nothing to stop.")
        self.manager.stop(job, "job_stop")
        return ToolResult(f"Stopped {job.id} ({job.name}): {job.state()}.")


def job_tools(config: Any = None) -> list[Any]:
    manager = JobManager(config)
    return [JobStart(manager), JobStatus(manager), JobOutput(manager), JobStop(manager)]
