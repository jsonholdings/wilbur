"""Loop mode: keep Wilbur working until the goal is met, and keep him busy.

Why this exists: `Agent.run` ends the moment the model sends a reply with no
tool calls (agent.py, the `if not reply.tool_calls: break`), and the REPL then
blocks on the next line of input. Nothing re-issues the objective, so a local
model that says "I'll do X next" and stops is simply finished. Two drivers
close that gap:

- `run_goal_loop` -- one goal, turn after turn on the SAME agent, until a
  done-check passes, a turn or wall-clock cap is hit, the agent stalls, or the
  caller cancels (REPL `/loop <goal>`).
- `run_queue` -- headless `wilbur loop --queue FILE`: take the next task, run
  it in a `wilbur/<slug>` worktree branch under a gpu-queue `external_lease`,
  commit there, record the result, take the next; back off while the queue is
  empty. It never merges or pushes: the owner reviews the branch.

The done-check is two-sided on purpose: the model must say the goal is
complete (the GOAL_COMPLETE marker) AND, when there is a real check (the
task's test commands, or the project's own check from `verify.detect`), that
check must pass. A model's word alone only counts when there is no check at
all, and the result then says "unverified".
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import verify, worktree
from .agent import Agent
from .config import Config
from .tools.base import Approval

GOAL_COMPLETE = "GOAL COMPLETE"
STALL_LIMIT = 2  # consecutive turns with no tool call and no completion

LOOP_SYSTEM = f"""You are running in LOOP MODE. You will be prompted again and again
until the goal is met, so do not stop to ask whether to continue: take the next
concrete step with your tools every turn. When, and only when, the goal is fully
met and you have verified it with a real command, end your reply with a line that
says exactly: {GOAL_COMPLETE}
Never run git push, git merge, git rebase, or check out another branch."""

CONTINUE_PROMPT = """Loop mode, turn {turn}. The goal is not met yet{why}.
Goal: {goal}
Take the next concrete step with your tools. End with "{marker}" only once it is done and verified."""


@dataclass
class LoopResult:
    status: str  # done | unverified | turn_cap | time_cap | stalled | cancelled | error
    turns: int = 0
    seconds: float = 0.0
    final: str = ""
    check: str = ""  # last done-check summary line, "" when there was none

    @property
    def ok(self) -> bool:
        return self.status in {"done", "unverified"}


def says_complete(reply: str) -> bool:
    return any(line.strip().strip("*`#. ").upper() == GOAL_COMPLETE
               for line in (reply or "").splitlines())


def run_commands(commands: list[list[str]], cwd: str, timeout: int = 600) -> tuple[bool, str]:
    """Run argv test commands in order; all must exit 0. `{python}` -> this interpreter
    (the routine-runner queue/1 placeholder)."""
    for argv in commands:
        argv = [sys.executable if a == "{python}" else a for a in argv]
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{' '.join(argv)}: {type(exc).__name__}: {exc}"
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr)[-1500:]
            return False, f"{' '.join(argv)} exited {proc.returncode}\n{tail}"
    return True, f"{len(commands)} command(s) passed"


def project_check(cwd: str) -> Callable[[], tuple[bool | None, str]] | None:
    """The project's own check as a done-check, or None when it has none."""
    check = verify.detect(cwd)
    if check is None:
        return None

    def run() -> tuple[bool | None, str]:
        passed, output = verify.run(check, cwd)
        return passed, verify.summarise(check, passed, output).splitlines()[0]
    return run


def run_goal_loop(
    agent: Agent,
    goal: str,
    *,
    max_turns: int = 20,
    max_seconds: float = 3600.0,
    check: Callable[[], tuple[bool | None, str]] | None = None,
    cancel: threading.Event | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_turn: Callable[[int, str], None] | None = None,
) -> LoopResult:
    """Drive `agent` turn after turn until the done-check passes or a cap stops it.

    A KeyboardInterrupt (Ctrl-C) propagates to the caller after cancelling the
    agent, so the REPL's own handler reports it; `cancel` stops it between turns.
    """
    cancel = cancel or threading.Event()
    started = clock()
    stalls = 0
    reply = ""
    check_line = ""
    why = ""
    for turn in range(1, max_turns + 1):
        if cancel.is_set():
            return LoopResult("cancelled", turn - 1, clock() - started, reply, check_line)
        if clock() - started >= max_seconds:
            return LoopResult("time_cap", turn - 1, clock() - started, reply, check_line)
        prompt = (f"{goal}\n\n(Loop mode: keep going until done; end with "
                  f"\"{GOAL_COMPLETE}\" once verified.)" if turn == 1 else
                  CONTINUE_PROMPT.format(turn=turn, why=why, goal=goal, marker=GOAL_COMPLETE))
        before = sum(1 for m in agent.messages if m.get("role") == "tool")
        try:
            reply = agent.run(prompt)
        except KeyboardInterrupt:
            agent.cancel_event.set()
            raise
        if on_turn is not None:
            on_turn(turn, reply)
        if agent.cancel_event.is_set():
            return LoopResult("cancelled", turn, clock() - started, reply, check_line)
        used_tools = sum(1 for m in agent.messages if m.get("role") == "tool") > before
        if says_complete(reply):
            if check is None:
                return LoopResult("unverified", turn, clock() - started, reply, "")
            passed, check_line = check()
            if passed is not False:
                return LoopResult("done", turn, clock() - started, reply, check_line)
            why = f" (you said it was done, but the check failed: {check_line[:300]})"
            stalls = 0
            continue
        why = ""
        stalls = 0 if used_tools else stalls + 1
        if stalls >= STALL_LIMIT:
            return LoopResult("stalled", turn, clock() - started, reply, check_line)
    return LoopResult("turn_cap", max_turns, clock() - started, reply, check_line)


# --------------------------------------------------------------------------- #
# Headless queue
# --------------------------------------------------------------------------- #

QUEUE_SCHEMAS = {"wilbur/queue/1", "routine-runner/queue/1"}
_SLUG = re.compile(r"[^a-z0-9-]+")


@dataclass
class QueueItem:
    id: str
    task: str
    branch: str
    acceptance_criteria: list[str] = field(default_factory=list)
    test_commands: list[list[str]] = field(default_factory=list)
    team: str = "auto"  # on | off | auto


def slug(text: str) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:48] or "task"


def load_queue(path: str) -> list[QueueItem]:
    """Read a routine-runner queue/1 (or wilbur/queue/1) JSON file.

    Only the fields Wilbur uses are read; a routine-runner spec's other keys are
    ignored. Branches are forced into `wilbur/`: whatever the file says, loop
    mode only ever commits on a `wilbur/<slug>` branch.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("schema") not in QUEUE_SCHEMAS:
        raise ValueError(f"{path}: schema must be one of {sorted(QUEUE_SCHEMAS)}")
    items: list[QueueItem] = []
    seen: set[str] = set()
    for i, raw in enumerate(data.get("items") or []):
        item_id = str(raw.get("id") or "").strip()
        task = str(raw.get("task") or "").strip()
        if not item_id or not task:
            raise ValueError(f"{path}: items[{i}] needs an id and a task")
        if item_id in seen:
            raise ValueError(f"{path}: duplicate item id {item_id!r}")
        seen.add(item_id)
        commands = raw.get("test_commands") or []
        if not all(isinstance(c, list) and c and all(isinstance(a, str) for a in c)
                   for c in commands):
            raise ValueError(f"{path}: items[{i}].test_commands must be argv lists")
        name = str(raw.get("branch") or item_id).split("/")[-1]
        team = str(raw.get("team", "auto")).lower()
        team = {"true": "on", "false": "off"}.get(team, team)
        items.append(QueueItem(
            id=item_id, task=task, branch=f"wilbur/{slug(name)}",
            acceptance_criteria=[str(c) for c in raw.get("acceptance_criteria") or []],
            test_commands=commands, team=team if team in {"on", "off", "auto"} else "auto",
        ))
    return items


def results_path(queue_path: str) -> str:
    return queue_path + ".results.jsonl"


def recorded_ids(queue_path: str) -> set[str]:
    path = results_path(queue_path)
    if not os.path.exists(path):
        return set()
    done: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                done.add(json.loads(line)["id"])
            except (ValueError, KeyError):
                continue
    return done


def record(queue_path: str, row: dict) -> None:
    with open(results_path(queue_path), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def next_item(queue_path: str) -> QueueItem | None:
    taken = recorded_ids(queue_path)
    for item in load_queue(queue_path):
        if item.id not in taken:
            return item
    return None


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _task_prompt(item: QueueItem) -> str:
    text = item.task
    if item.acceptance_criteria:
        text += "\n\nAcceptance criteria:\n" + "\n".join(f"- {c}" for c in item.acceptance_criteria)
    return text


class TaskRunner:
    """Runs one queue item in its own `wilbur/<slug>` worktree; never merges or pushes."""

    def __init__(self, config: Config, repo_dir: str, *, max_turns: int, max_seconds: float,
                 on_event: Callable[[str, dict], None] | None = None,
                 cancel: threading.Event | None = None) -> None:
        self.config = config
        self.repo_dir = os.path.abspath(repo_dir)
        self.max_turns = max_turns
        self.max_seconds = max_seconds
        self.on_event = on_event or (lambda kind, data: None)
        self.cancel = cancel or threading.Event()

    def run(self, item: QueueItem) -> dict:
        head_before = _git(self.repo_dir, "rev-parse", "HEAD")
        from . import team as team_mod
        use_team = team_mod.needs_team(item.task, item.team)
        row = (self._team(item) if use_team else self._single(item))
        row["mode"] = "team" if use_team else "single"
        # The owner's checked-out branch must be exactly where it was. Loop code
        # never merges; this catches a model that ran git itself.
        if _git(self.repo_dir, "rev-parse", "HEAD") != head_before:
            row["status"] = "violation"
            row["note"] = "the checked-out branch moved during the task; review by hand"
        return row

    def _team(self, item: QueueItem) -> dict:
        from .team import TeamOrchestrator
        check = None
        if item.test_commands:
            # The queue's own acceptance commands are the gate, as in single mode;
            # without them the orchestrator detects the project's check itself.
            def check(wt_path: str) -> tuple[bool | None, str]:
                return run_commands(item.test_commands, wt_path)
        orch = TeamOrchestrator(self.config, self.repo_dir,
                                approve=lambda tool, detail: Approval.GRANTED,
                                on_event=self.on_event, cancel=self.cancel,
                                check=check,
                                check_label=f"{len(item.test_commands)} queue command(s)")
        result = orch.run(_task_prompt(item), branch=item.branch, merge=False)
        status = {"approved": "done"}.get(result.status, result.status)
        row = {"status": status, "branch": item.branch,
               "commit": _git(self.repo_dir, "rev-parse", "--verify", "-q", item.branch),
               "summary": result.summary, "revisions": result.revisions,
               "red_green": result.red_green,
               "tester_reverted": result.tester_reverted,
               "tester_calls": result.tester_calls,
               "verdicts": result.verdicts,
               "roles": [[role, secs] for role, secs in result.role_seconds]}
        if result.reviewer_model_note:
            row["reviewer_model_note"] = result.reviewer_model_note
        if result.gate_results:
            row["check"] = "; ".join(f"{g.label}: {'PASS' if g.passed else 'FAIL'}"
                                     for g in result.gate_results)
        return row

    def _single(self, item: QueueItem) -> dict:
        wt_path = worktree.create(self.repo_dir, item.branch)
        try:
            agent = Agent(self.config, wt_path,
                          approve=lambda tool, detail: Approval.GRANTED,
                          on_event=self.on_event, system_extra=LOOP_SYSTEM,
                          cancel=self.cancel)
            if item.test_commands:
                def check() -> tuple[bool | None, str]:
                    return run_commands(item.test_commands, wt_path)
            else:
                check = project_check(wt_path)
            result = run_goal_loop(agent, _task_prompt(item), max_turns=self.max_turns,
                                   max_seconds=self.max_seconds, check=check,
                                   cancel=self.cancel)
            subprocess.run(["git", "-C", wt_path, "add", "-A"], capture_output=True)
            subprocess.run(["git", "-C", wt_path, "commit", "-q", "-m",
                            f"wilbur loop: {item.id} ({result.status})\n\n{item.task[:500]}"],
                           capture_output=True)
            commit = _git(wt_path, "rev-parse", "HEAD")
            return {"status": result.status, "branch": item.branch, "commit": commit,
                    "turns": result.turns, "check": result.check,
                    "summary": result.final[-600:]}
        finally:
            worktree.remove(self.repo_dir, wt_path, branch=None, force=True)


LeaseFn = Callable[[Callable[[], dict], QueueItem], dict]


def gq_lease(config: Config, *, admit_timeout: float = 600.0) -> LeaseFn:
    """Each task holds one gpu-queue `external_lease` (FIFO admission: it waits its
    turn and never preempts a running job), heartbeats while it works, unloads
    Wilbur's model, and releases before the next task. Raises gpu_lease.Deferred
    when not admitted in time (the caller backs off and retries the same item)."""
    from . import gpu_lease  # private build; vendored from infrastructure/gpu-lease
    from .models import ModelManager
    client = gpu_lease.QueueClient(app="wilbur", process="loop")

    def lease(work: Callable[[], dict], item: QueueItem) -> dict:
        def unload() -> None:
            ModelManager(config.base_url).unload_all()
        row = gpu_lease.run_leased(work, client, real_kind="wilbur_task", model=config.model,
                                   why=f"wilbur loop: {item.id}", extra={"submitter": "wilbur"},
                                   admit_timeout=admit_timeout, after_work=unload)
        return row
    return lease


def no_lease(work: Callable[[], dict], item: QueueItem) -> dict:
    return work()


def _is_deferred(exc: BaseException) -> bool:
    return type(exc).__name__ == "Deferred"


def run_queue(
    queue_path: str,
    repo_dir: str,
    runner: TaskRunner,
    lease: LeaseFn,
    *,
    once: bool = False,
    max_tasks: int = 0,
    idle_min: float = 30.0,
    idle_max: float = 600.0,
    stop: threading.Event | None = None,
    sleep: Callable[[float], None] | None = None,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr, flush=True),
    outcome: dict | None = None,
) -> int:
    """Work the queue until stopped. Returns the number of tasks recorded.

    The queue file is re-read every cycle, so tasks appended while it runs are
    picked up. An empty queue backs off idle_min -> idle_max (doubling); `once`
    returns instead of waiting. `outcome`, when given, is filled with
    `lease_failures` (count) and `lease_error` (the last one), so a caller can
    tell "queue empty" from "could not get the GPU" -- both record 0 tasks.
    """
    if outcome is None:
        outcome = {}
    outcome.setdefault("lease_failures", 0)
    outcome.setdefault("lease_error", "")
    stop = stop or threading.Event()
    sleep = sleep or stop.wait
    backoff = idle_min
    recorded = 0
    while not stop.is_set():
        item = next_item(queue_path)
        if item is None:
            if once:
                break
            log(f"wilbur loop: queue empty, next look in {backoff:.0f}s")
            sleep(backoff)
            backoff = min(idle_max, backoff * 2)
            continue
        log(f"wilbur loop: task {item.id} -> {item.branch}")
        started = time.time()

        finished: dict = {}

        def work(item: QueueItem = item) -> dict:
            # A task's own failure is a result to record; only a LEASE failure
            # (queue busy, unreachable, refused) escapes, and that is retried.
            try:
                finished["row"] = runner.run(item)
            except Exception as exc:  # noqa: BLE001 -- recorded, the loop goes on
                finished["row"] = {"status": "error", "branch": item.branch,
                                   "summary": f"{type(exc).__name__}: {exc}"}
            return finished["row"]
        try:
            row = lease(work, item)
        except Exception as exc:  # noqa: BLE001 -- never burn the queue on a GPU-queue outage
            if "row" in finished:
                # The task ran; only the release failed (gq went away mid-task).
                # Record it so it is not re-run; the unheartbeated lease expires
                # on gpu-queue's TTL (gpu-queue lease.py DEFAULT_LEASE_TTL_SECONDS).
                row = finished["row"]
                row["lease_note"] = f"lease release failed: {type(exc).__name__}: {exc}"
                log(f"wilbur loop: {item.id} ran, but {row['lease_note']}")
            else:
                why = "waiting for the GPU" if _is_deferred(exc) else "gpu-queue lease failed"
                outcome["lease_failures"] += 1
                outcome["lease_error"] = f"{why}: {type(exc).__name__}: {exc}"
                log(f"wilbur loop: {item.id} {why} ({type(exc).__name__}: {exc}); "
                    + ("not run (--once)" if once else "retrying later"))
                if once:
                    break
                sleep(backoff)
                backoff = min(idle_max, backoff * 2)
                continue
        row.update(id=item.id, seconds=round(time.time() - started, 1),
                   finished=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        record(queue_path, row)
        recorded += 1
        backoff = idle_min
        log(f"wilbur loop: {item.id} {row['status']} ({row.get('commit', '')[:12]})")
        if row.get("roles"):
            log(f"wilbur loop: {item.id} roles "
                + ", ".join(f"{r} {secs}s" for r, secs in row["roles"])
                + f"; red/green {row.get('red_green') or '-'}"
                + (f"; verdicts {' -> '.join(row['verdicts'])}" if row.get("verdicts") else ""))
        if row.get("reviewer_model_note"):
            log(f"wilbur loop: {item.id} {row['reviewer_model_note']}")
        for call in row.get("tester_calls") or []:
            log(f"wilbur loop: {item.id} {call['role']} {call['name']}({call['args']}) "
                f"-> {call['status']}")
        if max_tasks and recorded >= max_tasks:
            break
    return recorded


def loop_main(argv: list[str]) -> int:
    import argparse
    import signal

    parser = argparse.ArgumentParser(
        prog="wilbur loop",
        description="Work a task queue headless: each task on its own wilbur/<slug> "
                    "branch, under a gpu-queue lease. Never merges or pushes.")
    parser.add_argument("--queue", required=True, help="queue JSON (wilbur/queue/1 or "
                        "routine-runner/queue/1); results go to <queue>.results.jsonl")
    parser.add_argument("-C", "--repo", default=os.getcwd(), help="git repo the tasks run in")
    parser.add_argument("-m", "--model", help="model to use")
    parser.add_argument("--max-turns", type=int, default=20, help="loop turns per task")
    parser.add_argument("--max-minutes", type=float, default=45, help="wall clock per task")
    parser.add_argument("--max-tasks", type=int, default=0, help="stop after N tasks (0 = never)")
    parser.add_argument("--once", action="store_true", help="exit when the queue is empty")
    parser.add_argument("--idle-max", type=float, default=600, help="longest empty-queue back-off, s")
    parser.add_argument("--admit-timeout", type=float, default=600,
                        help="seconds to wait for a gpu-queue lease before backing off")
    parser.add_argument("--no-lease", action="store_true",
                        help="run without gpu-queue (only where nothing else uses the GPU)")
    args = parser.parse_args(argv)

    if not os.path.isdir(os.path.join(args.repo, ".git")) and not _git(args.repo, "rev-parse", "--git-dir"):
        print(f"wilbur loop: {args.repo} is not a git repository", file=sys.stderr)
        return 2
    try:
        load_queue(args.queue)
    except (OSError, ValueError) as exc:
        print(f"wilbur loop: {exc}", file=sys.stderr)
        return 2

    config = Config.load()
    if args.model:
        config.model = args.model
        config.subagent_model = args.model
    if args.no_lease:
        lease = no_lease
    else:
        try:
            lease = gq_lease(config, admit_timeout=args.admit_timeout)
        except Exception as exc:  # noqa: BLE001 -- no queue, no secret, or the OSS build
            print(f"wilbur loop: gpu-queue lease unavailable ({type(exc).__name__}: {exc}); "
                  "refusing to load models behind the queue. --no-lease overrides.",
                  file=sys.stderr)
            return 2

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    runner = TaskRunner(config, args.repo, max_turns=args.max_turns,
                        max_seconds=args.max_minutes * 60, cancel=stop)
    outcome: dict = {}
    try:
        n = run_queue(args.queue, args.repo, runner, lease, once=args.once,
                      max_tasks=args.max_tasks, idle_max=args.idle_max, stop=stop,
                      outcome=outcome)
    except KeyboardInterrupt:
        stop.set()
        print("wilbur loop: stopped", file=sys.stderr)
        return 130
    print(f"wilbur loop: {n} task(s) recorded in {results_path(args.queue)}", file=sys.stderr)
    if args.once and outcome.get("lease_failures") and next_item(args.queue) is not None:
        # A --once run that stopped on a lease failure did NOT find an empty
        # queue: a pending task never ran. Exit 2 (UNKNOWN), never 0.
        print(f"wilbur loop: UNKNOWN -- a pending task did not run ({outcome['lease_error']}); "
              "exit 2", file=sys.stderr)
        return 2
    return 0


__all__ = ["GOAL_COMPLETE", "LoopResult", "QueueItem", "TaskRunner", "load_queue",
           "loop_main", "next_item", "run_goal_loop", "run_queue", "says_complete"]
