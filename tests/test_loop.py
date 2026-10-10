"""Loop mode (wilbur/loop.py) on the fake backend: no Ollama, no gpu-queue."""
import json
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tests.conftest import call, say
from wilbur import loop
from wilbur.config import Config
from wilbur.llm import Reply

WRITE = Reply(content="working", tool_calls=[call("write_file", path="out.txt", content="ok\n")])
DONE = say(f"All set.\n{loop.GOAL_COMPLETE}")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    (r / "README.md").write_text("hello\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _queue(tmp_path, items):
    q = tmp_path / "queue.json"
    q.write_text(json.dumps({"schema": "wilbur/queue/1", "items": items}))
    return str(q)


# ---------------------------------------------------------------- goal loop

def test_text_only_reply_no_longer_ends_the_work(make_agent):
    """The root cause: a reply with no tool call ended the run. The loop re-prompts."""
    agent, client = make_agent([say("I'll write the file next."), WRITE, say("written"), DONE])
    result = loop.run_goal_loop(agent, "write out.txt", max_turns=5)
    assert result.status == "unverified"  # no check given: the model's word only
    assert result.turns == 3
    assert "Loop mode, turn 2" in client.seen[1][-1]["content"]


def test_done_needs_the_check_to_pass(make_agent):
    agent, _ = make_agent([DONE, WRITE, DONE])
    state = {"n": 0}

    def check():
        state["n"] += 1
        return (state["n"] > 1), f"check run {state['n']}"
    result = loop.run_goal_loop(agent, "g", max_turns=5, check=check)
    assert result.status == "done" and result.turns == 2 and state["n"] == 2


def test_failed_check_is_fed_back(make_agent):
    agent, client = make_agent([DONE, DONE])
    result = loop.run_goal_loop(agent, "g", max_turns=2, check=lambda: (False, "make test exited 2"))
    assert result.status == "turn_cap"
    assert "make test exited 2" in client.seen[1][-1]["content"]


def test_turn_cap(make_agent):
    agent, _ = make_agent([WRITE, say("more"), WRITE, say("more")] * 3)
    assert loop.run_goal_loop(agent, "g", max_turns=2).status == "turn_cap"


def test_wall_clock_cap(make_agent):
    agent, _ = make_agent([WRITE, say("more")] * 5)
    ticks = iter([0, 0, 100, 100, 100])
    result = loop.run_goal_loop(agent, "g", max_turns=10, max_seconds=50,
                                clock=lambda: next(ticks))
    assert result.status == "time_cap" and result.turns == 1


def test_stalls_after_turns_with_no_tool_call(make_agent):
    agent, _ = make_agent([say("hmm"), say("hmm"), say("hmm")])
    result = loop.run_goal_loop(agent, "g", max_turns=10)
    assert result.status == "stalled" and result.turns == loop.STALL_LIMIT


def test_cancel_event_stops_between_turns(make_agent):
    agent, _ = make_agent([WRITE, say("more")] * 3)
    cancel = threading.Event()
    result = loop.run_goal_loop(agent, "g", max_turns=10, cancel=cancel,
                                on_turn=lambda t, r: cancel.set())
    assert result.status == "cancelled" and result.turns == 1


def test_ctrl_c_propagates_and_cancels_agent(make_agent):
    agent, _ = make_agent([])

    def boom(_prompt):
        raise KeyboardInterrupt
    agent.run = boom
    with pytest.raises(KeyboardInterrupt):
        loop.run_goal_loop(agent, "g")
    assert agent.cancel_event.is_set()


def test_says_complete_tolerates_markdown():
    assert loop.says_complete("done\n**GOAL COMPLETE**")
    assert not loop.says_complete("the goal complete criteria are not met")


# ---------------------------------------------------------------- queue file

def test_load_queue_forces_wilbur_branches_and_reads_routine_runner_spec(tmp_path):
    q = tmp_path / "q.json"
    q.write_text(json.dumps({"schema": "routine-runner/queue/1", "repo": "x", "items": [
        {"id": "a", "task": "do a", "branch": "cloud/Fix Thing", "max_diff_lines": 9,
         "test_commands": [["{python}", "-c", "pass"]], "acceptance_criteria": ["c1"]},
    ]}))
    (item,) = loop.load_queue(str(q))
    assert item.branch == "wilbur/fix-thing"
    assert item.test_commands == [["{python}", "-c", "pass"]]


@pytest.mark.parametrize("bad", [
    {"schema": "other", "items": []},
    {"schema": "wilbur/queue/1", "items": [{"id": "a"}]},
    {"schema": "wilbur/queue/1", "items": [{"id": "a", "task": "t"}, {"id": "a", "task": "u"}]},
    {"schema": "wilbur/queue/1", "items": [{"id": "a", "task": "t", "test_commands": ["pytest -q"]}]},
])
def test_load_queue_refuses_bad_specs(tmp_path, bad):
    q = tmp_path / "q.json"
    q.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        loop.load_queue(str(q))


def test_run_commands_substitutes_python(tmp_path):
    ok, _ = loop.run_commands([["{python}", "-c", "import sys; sys.exit(0)"]], str(tmp_path))
    bad, why = loop.run_commands([["{python}", "-c", "import sys; sys.exit(3)"]], str(tmp_path))
    assert ok and not bad and "exited 3" in why


# ---------------------------------------------------------------- headless queue

def _runner(repo, **kw):
    cfg = Config()
    cfg.max_turns = 5
    return loop.TaskRunner(cfg, str(repo), max_turns=kw.get("max_turns", 4), max_seconds=600)


def test_queue_task_commits_only_on_its_wilbur_branch(repo, tmp_path, fake_client_factory):
    fake_client_factory([WRITE, DONE])
    head = _git(repo, "rev-parse", "HEAD")
    q = _queue(tmp_path, [{"id": "t1", "task": "write out.txt", "team": "off",
                           "test_commands": [["test", "-f", "out.txt"]]}])
    n = loop.run_queue(q, str(repo), _runner(repo), loop.no_lease, once=True)
    assert n == 1
    row = json.loads(Path(q + ".results.jsonl").read_text())
    assert row["status"] == "done" and row["branch"] == "wilbur/t1" and row["mode"] == "single"
    assert _git(repo, "rev-parse", "HEAD") == head          # owner's branch untouched
    assert _git(repo, "branch", "--show-current") != "wilbur/t1"
    assert _git(repo, "show", "wilbur/t1:out.txt") == "ok"
    assert row["commit"] == _git(repo, "rev-parse", "wilbur/t1")
    assert not (repo / ".wilbur-worktrees" / "wilbur" / "t1").exists()


def test_queue_continues_to_next_task_and_skips_recorded(repo, tmp_path, fake_client_factory):
    fake_client_factory([WRITE, DONE, WRITE, DONE])
    q = _queue(tmp_path, [
        {"id": "a", "task": "one", "team": "off", "test_commands": [["true"]]},
        {"id": "b", "task": "two", "team": "off", "test_commands": [["true"]]},
    ])
    assert loop.run_queue(q, str(repo), _runner(repo), loop.no_lease, once=True) == 2
    assert loop.run_queue(q, str(repo), _runner(repo), loop.no_lease, once=True) == 0
    ids = [json.loads(l)["id"] for l in Path(q + ".results.jsonl").read_text().splitlines()]
    assert ids == ["a", "b"]


def test_idle_backoff_doubles_to_cap_then_stops(tmp_path, repo):
    q = _queue(tmp_path, [])
    stop = threading.Event()
    waits = []

    def sleep(s):
        waits.append(s)
        if len(waits) == 5:
            stop.set()
    loop.run_queue(q, str(repo), _runner(repo), loop.no_lease, idle_min=30, idle_max=100,
                   stop=stop, sleep=sleep, log=lambda s: None)
    assert waits == [30, 60, 100, 100, 100]


def test_lease_wraps_each_task_and_deferral_is_retried_not_recorded(repo, tmp_path,
                                                                    fake_client_factory):
    fake_client_factory([WRITE, DONE])
    q = _queue(tmp_path, [{"id": "a", "task": "one", "team": "off", "test_commands": [["true"]]}])

    class Deferred(Exception):
        pass
    calls = []

    def lease(work, item):
        calls.append(item.id)
        if len(calls) == 1:
            raise Deferred("not admitted within 600s")
        return work()
    stop = threading.Event()
    loop.run_queue(q, str(repo), _runner(repo), lease, stop=stop, max_tasks=1,
                   sleep=lambda s: None, log=lambda s: None)
    assert calls == ["a", "a"]
    rows = Path(q + ".results.jsonl").read_text().splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["status"] == "done"


def test_task_exception_is_recorded_and_loop_goes_on(repo, tmp_path, monkeypatch):
    q = _queue(tmp_path, [{"id": "a", "task": "t", "team": "off"},
                          {"id": "b", "task": "u", "team": "off"}])
    runner = _runner(repo)
    monkeypatch.setattr(runner, "_single", lambda item: (_ for _ in ()).throw(RuntimeError("x")))
    assert loop.run_queue(q, str(repo), runner, loop.no_lease, once=True) == 2
    rows = [json.loads(l) for l in Path(q + ".results.jsonl").read_text().splitlines()]
    assert [r["status"] for r in rows] == ["error", "error"]


def test_moved_owner_branch_is_flagged(repo, tmp_path, monkeypatch):
    runner = _runner(repo)

    def sneaky(item):
        (repo / "x").write_text("x")
        _git(repo, "add", "x")
        _git(repo, "commit", "-q", "-m", "model ran git")
        return {"status": "done", "branch": item.branch}
    monkeypatch.setattr(runner, "_single", sneaky)
    row = runner.run(loop.QueueItem(id="a", task="t", branch="wilbur/a", team="off"))
    assert row["status"] == "violation"


def test_team_task_is_never_merged(repo, tmp_path, fake_client_factory):
    (repo / "Makefile").write_text("test:\n\t@test -f .ok\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "make")
    head = _git(repo, "rev-parse", "HEAD")
    fake_client_factory([
        say("Plan.\n\nAcceptance criteria:\n- x"),
        Reply(content="t", tool_calls=[call("write_file", path="tests/t.txt", content="t\n")]),
        say("tests"),
        Reply(content="c", tool_calls=[call("write_file", path=".ok", content="ok\n")]),
        say("done"),
        say("VERDICT: APPROVE"),
    ])
    q = _queue(tmp_path, [{"id": "big", "task": "refactor it", "team": "on"}])
    loop.run_queue(q, str(repo), _runner(repo), loop.no_lease, once=True)
    row = json.loads(Path(q + ".results.jsonl").read_text())
    assert row["mode"] == "team" and row["status"] == "done", row
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "show", "wilbur/big:.ok") == "ok"


def test_cli_dispatch_refuses_without_lease_when_queue_absent(tmp_path, repo, monkeypatch):
    """`wilbur loop` refuses to load models behind gpu-queue's back."""
    from wilbur import __main__ as main_mod
    q = _queue(tmp_path, [])

    def no_queue(*a, **kw):
        raise RuntimeError("gpu-queue secret unavailable")
    monkeypatch.setattr(loop, "gq_lease", no_queue)
    assert main_mod.main(["loop", "--queue", q, "-C", str(repo), "--once"]) == 2
    assert main_mod.main(["loop", "--queue", q, "-C", str(repo), "--once", "--no-lease"]) == 0


def _refused(*a, **kw):
    import urllib.error
    raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


def test_once_lease_failure_is_counted_not_recorded(repo, tmp_path):
    q = _queue(tmp_path, [{"id": "a", "task": "t", "team": "off"}])
    outcome = {}
    n = loop.run_queue(q, str(repo), _runner(repo), lambda work, item: _refused(),
                       once=True, log=lambda s: None, outcome=outcome)
    assert n == 0 and outcome["lease_failures"] == 1
    assert "Connection refused" in outcome["lease_error"]
    assert not Path(q + ".results.jsonl").exists()      # the task stays pending


def test_cli_once_exits_2_when_lease_refused(repo, tmp_path, monkeypatch, capsys):
    """2026-10-02: a refused gq lease exited 0 with "0 task(s) recorded"."""
    from wilbur import __main__ as main_mod
    q = _queue(tmp_path, [{"id": "a", "task": "t", "team": "off"}])
    monkeypatch.setattr(loop, "gq_lease", lambda config, **kw: (lambda work, item: _refused()))
    assert main_mod.main(["loop", "--queue", q, "-C", str(repo), "--once"]) == 2
    assert "UNKNOWN" in capsys.readouterr().err
    assert loop.next_item(q).id == "a"


def test_cli_once_empty_queue_still_exits_0(repo, tmp_path, monkeypatch):
    from wilbur import __main__ as main_mod
    q = _queue(tmp_path, [])
    monkeypatch.setattr(loop, "gq_lease", lambda config, **kw: (lambda work, item: _refused()))
    assert main_mod.main(["loop", "--queue", q, "-C", str(repo), "--once"]) == 0


def test_release_failure_after_work_is_recorded_once(repo, tmp_path, monkeypatch):
    q = _queue(tmp_path, [{"id": "a", "task": "t", "team": "off"}])
    runner = _runner(repo)
    monkeypatch.setattr(runner, "run", lambda item: {"status": "done", "branch": item.branch})

    def lease(work, item):
        work()
        _refused()                                     # complete() hit a dead gq
    outcome = {}
    assert loop.run_queue(q, str(repo), runner, lease, once=True, log=lambda s: None,
                          outcome=outcome) == 1
    row = json.loads(Path(q + ".results.jsonl").read_text())
    assert row["status"] == "done" and "lease release failed" in row["lease_note"]
    assert outcome["lease_failures"] == 0 and loop.next_item(q) is None


class _GqClient:
    def __init__(self, status_exc=None, cancel_exc=None):
        self.calls, self.status_exc, self.cancel_exc = [], status_exc, cancel_exc

    def submit(self, *a, **kw):
        self.calls.append("submit")
        return "job1"

    def status(self, job_id):
        self.calls.append("status")
        if self.status_exc:
            raise self.status_exc
        return "queued"

    def cancel(self, job_id):
        self.calls.append("cancel")
        if self.cancel_exc:
            raise self.cancel_exc

    def complete(self, *a, **kw):
        self.calls.append("complete")

    def heartbeat(self, job_id):
        pass


@pytest.mark.parametrize("client", [
    _GqClient(status_exc=OSError("Connection refused")),                       # poll fails
    _GqClient(status_exc=OSError("refused"), cancel_exc=OSError("refused")),   # cancel fails too
    _GqClient(),                                                               # never admitted
])
def test_submitted_lease_is_cancelled_on_every_failure_path(client):
    gpu_lease = pytest.importorskip("wilbur.gpu_lease")
    ran = []
    with pytest.raises(Exception):
        gpu_lease.run_leased(lambda: ran.append(1), client, real_kind="wilbur_task", model="m",
                             why="t", admit_timeout=0, sleep=lambda s: None)
    # Cancel is always attempted; when it fails the lease was never admitted and
    # never heartbeats, so gpu-queue's lease TTL expires it once admitted.
    assert ran == [] and client.calls[0] == "submit" and client.calls[-1] == "cancel"
    assert "complete" not in client.calls


def test_cli_rejects_non_repo_and_bad_queue(tmp_path):
    from wilbur import __main__ as main_mod
    q = _queue(tmp_path, [])
    assert main_mod.main(["loop", "--queue", q, "-C", str(tmp_path), "--no-lease"]) == 2


def test_repl_loop_command_runs_to_done(monkeypatch, tmp_path, make_agent):
    from wilbur import repl as repl_mod
    agent, _ = make_agent([WRITE, DONE])
    r = repl_mod.Repl.__new__(repl_mod.Repl)
    r.agent, r.config, r.cwd, r._gp = agent, agent.config, str(tmp_path), None
    r._turn_active = threading.Event()
    r.sessions = type("S", (), {"save": lambda *a, **k: None})()
    r.session_id, r.state = "x", agent.state
    out = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a))))
    r._loop_command("write out.txt")
    assert any("loop: unverified after 1 turn(s)" in line for line in out), out
    assert loop.LOOP_SYSTEM in agent.messages[0]["content"]


def test_team_task_gates_on_the_queue_commands_and_records_roles(repo, tmp_path, fake_client_factory):
    # No project check in this repo: only the queue's own command can gate it.
    fake_client_factory([
        say("Plan.\n\nAcceptance criteria:\n- out.txt exists"),
        Reply(content="t", tool_calls=[call("write_file", path="tests/test_out.txt",
                                            content="t\n")]),
        say("tests"),
        Reply(content="c", tool_calls=[call("write_file", path="out.txt", content="ok\n")]),
        say("done"),
        say("VERDICT: APPROVE"),
    ])
    q = _queue(tmp_path, [{"id": "gated", "task": "write out.txt", "team": "on",
                           "test_commands": [["test", "-f", "out.txt"]]}])
    logged = []
    loop.run_queue(q, str(repo), _runner(repo), loop.no_lease, once=True, log=logged.append)
    row = json.loads(Path(q + ".results.jsonl").read_text())
    assert row["status"] == "done", row
    assert row["red_green"] == "red->green"
    assert row["tester_calls"] == [{"role": "tester", "name": "write_file",
                                    "args": "path=tests/test_out.txt, content=t",
                                    "status": "ok"}]
    assert any("tester write_file(path=tests/test_out.txt" in line and "-> ok" in line
               for line in logged), logged
    assert any("roles planner" in line and "red/green red->green" in line
               for line in logged), logged
    assert row["check"] == "1 queue command(s): PASS"
    assert [r for r, _ in row["roles"]] == ["planner", "tester", "coder", "reviewer"]
