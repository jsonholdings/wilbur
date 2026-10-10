"""Team mode: size gate, plan -> code -> test -> review -> merge, on the fake
backend only (no live Ollama calls needed).

Every scripted `FakeClient` reply queue is consumed in the exact order
`TeamOrchestrator` issues `Agent.run()` calls for each role (planner, tester,
coder, reviewer, reviser...), since all `Agent` instances share one patched
`OllamaClient`. A gate is a real `make test` run against a marker file inside
the worktree -- deterministic, no dependency on a nested test runner.
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tests.conftest import call, say
from wilbur.config import Config
from wilbur.llm import Reply
from wilbur.team import TeamOrchestrator, needs_team
from wilbur.tools.base import Approval

MAKEFILE = """test:
\t@test -f .team_test_passed
"""

PLAN = say("Plan.\n\nAcceptance criteria:\n- x")
TESTER_WRITE = Reply(content="t", tool_calls=[
    call("write_file", path="tests/test_x.py", content="def test_x(): assert False\n"),
])
TESTER_DONE = say("failing test recorded")
CODER_WRITE_MARKER = Reply(content="c", tool_calls=[
    call("write_file", path=".team_test_passed", content="ok\n"),
])
CODER_WRITE_OTHER = Reply(content="c", tool_calls=[
    call("write_file", path="NOTES.txt", content="wip\n"),
])
CODER_DONE = say("implemented")
APPROVE = say("Checklist:\n- x: yes\n\nVERDICT: APPROVE")
DENY = say("not quite right\n\nVERDICT: DENY")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                    capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    (r / "Makefile").write_text(MAKEFILE)
    (r / "README.md").write_text("hello\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _orch(repo, config=None):
    return TeamOrchestrator(config or Config(), str(repo),
                             approve=lambda *_: Approval.GRANTED)


# --------------------------------------------------------------------- #
# size gate

def test_size_gate_on_off_are_explicit():
    assert needs_team("fix a typo", "on") is True
    assert needs_team("refactor the whole thing across the codebase", "off") is False


def test_size_gate_auto_trivial_vs_multi_file():
    assert needs_team("fix the typo in README.md", "auto") is False
    assert needs_team("update foo.py and bar.py to add logging", "auto") is True
    assert needs_team("refactor the auth module across the codebase", "auto") is True


# --------------------------------------------------------------------- #
# happy path

def test_happy_path_plan_code_test_review_merge(repo, fake_client_factory):
    client = fake_client_factory([
        PLAN, TESTER_WRITE, TESTER_DONE, CODER_WRITE_MARKER, CODER_DONE, APPROVE,
    ])
    result = _orch(repo).run("add a widget", branch="happy")
    assert result.status == "merged"
    assert result.merge_sha
    assert not (repo / ".wilbur-worktrees" / "happy").exists()
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"],
                          capture_output=True, text=True, check=True).stdout
    assert "team:" in log


def test_deny_then_revise_then_approve(repo, fake_client_factory):
    client = fake_client_factory([
        PLAN, TESTER_WRITE, TESTER_DONE, CODER_WRITE_MARKER, CODER_DONE,
        DENY, say("revised"), APPROVE,
    ])
    result = _orch(repo).run("add x", branch="revise")
    assert result.status == "merged"
    assert result.revisions == 1


def test_two_denials_then_ask_user(repo, fake_client_factory):
    config = Config()
    config.team_max_revisions = 2
    client = fake_client_factory([
        PLAN, TESTER_WRITE, TESTER_DONE, CODER_WRITE_MARKER, CODER_DONE,
        DENY, say("revised 1"), DENY, say("revised 2"), DENY,
    ])
    result = _orch(repo, config).run("add x", branch="denied")
    assert result.status == "denied_ask_user"
    assert result.revisions == 3
    assert not (repo / ".wilbur-worktrees" / "denied").exists()


def test_gate_failure_blocks_review(repo, fake_client_factory):
    client = fake_client_factory([
        PLAN, TESTER_WRITE, TESTER_DONE, CODER_WRITE_OTHER, CODER_DONE,
    ])
    result = _orch(repo).run("add x", branch="gatefail")
    assert result.status == "gate_failed"
    assert any(not g.passed for g in result.gate_results)
    # only planner(1) + tester(2) + coder(2) calls were made -- the reviewer
    # reply was never consumed because review was never reached.
    assert len(client.seen) == 5
    assert not (repo / ".wilbur-worktrees" / "gatefail").exists()


def test_worktree_cleanup_after_merge(repo, fake_client_factory):
    client = fake_client_factory([
        PLAN, TESTER_WRITE, TESTER_DONE, CODER_WRITE_MARKER, CODER_DONE, APPROVE,
    ])
    wt_path = repo / ".wilbur-worktrees" / "cleanup"
    result = _orch(repo).run("add x", branch="cleanup")
    assert result.status == "merged"
    assert not wt_path.exists()


def test_role_timeout_returns_cleanly(repo, monkeypatch):
    import wilbur.agent as agent_mod

    class HangingClient:
        def __call__(self, *a, **kw):
            return self

        def chat(self, *a, **kw):
            time.sleep(2)
            return say("too slow")

    monkeypatch.setattr(agent_mod, "OllamaClient", HangingClient())
    config = Config()
    config.team_role_timeout_s = 0.05
    orch = _orch(repo, config)
    started = time.time()
    result = orch.run("add x", branch="slow")
    elapsed = time.time() - started
    assert result.status == "timeout"
    assert elapsed < 1.5
    # planning happens before any worktree is created, so there is nothing
    # to clean up -- the run must still return cleanly either way.
    assert not (repo / ".wilbur-worktrees" / "slow").exists()


# --------------------------------------------------------------------- #
# 2.22.2: a timed-out or cancelled role stops calling the model; red/green
# and per-role timings are recorded (live run 2026-10-02 follow-ups)

class _LoopingClient:
    """Never finishes a turn: every reply is another tool call."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **kw):
        return self

    def chat(self, *a, **kw):
        self.calls += 1
        time.sleep(0.05)
        return Reply(content="", tool_calls=[call("list_files", path=".")])


def test_timed_out_role_stops_calling_the_model(repo, monkeypatch):
    import wilbur.agent as agent_mod

    client = _LoopingClient()
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    config = Config()
    config.max_turns = 10_000
    config.team_role_timeout_s = 0.3
    result = _orch(repo, config).run("add x", branch="abandoned")
    assert result.status == "timeout"
    time.sleep(0.3)  # let the abandoned thread notice its stop event
    settled = client.calls
    time.sleep(0.5)
    assert client.calls == settled, "abandoned role kept calling the model"
    assert result.role_seconds and result.role_seconds[0][0] == "planner"


def test_caller_cancel_stops_the_team(repo, monkeypatch):
    import threading
    import wilbur.agent as agent_mod

    client = _LoopingClient()
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    config = Config()
    config.max_turns = 10_000
    config.team_role_timeout_s = 60
    cancel = threading.Event()
    orch = TeamOrchestrator(config, str(repo), approve=lambda *_: Approval.GRANTED,
                            cancel=cancel)
    threading.Timer(0.3, cancel.set).start()
    started = time.time()
    result = orch.run("add x", branch="cancelled")
    assert result.status == "cancelled"
    assert time.time() - started < 5


def test_red_green_and_role_seconds_recorded(repo, fake_client_factory):
    fake_client_factory([
        PLAN, TESTER_WRITE, TESTER_DONE, CODER_WRITE_MARKER, CODER_DONE, APPROVE,
    ])
    result = _orch(repo).run("add a widget", branch="redgreen")
    assert result.status == "merged"
    assert result.red_green == "red->green"
    assert [r for r, _ in result.role_seconds] == ["planner", "tester", "coder", "reviewer"]


def test_tests_green_before_the_coder_reprompt_then_fail_tester_no_red(repo, fake_client_factory):
    client = fake_client_factory([
        PLAN, TESTER_WRITE, TESTER_DONE,
        TESTER_DONE,  # the re-prompt: the tester changes nothing
    ])
    orch = TeamOrchestrator(Config(), str(repo), approve=lambda *_: Approval.GRANTED,
                            check=lambda wt: (True, "always passes"), check_label="lenient")
    result = orch.run("add x", branch="nored")
    assert result.status == "tester_no_red"
    assert result.red_green == "no-red"
    assert [r for r, _ in result.role_seconds] == ["planner", "tester", "tester-retry"]
    retry_prompt = client.seen[-1][-1]["content"]
    assert "must fail against the current code" in retry_prompt


def test_no_red_reprompt_that_produces_a_failing_test_proceeds(repo, fake_client_factory):
    tester_retry_write = Reply(content="t", tool_calls=[
        call("write_file", path="tests/test_y.py", content="def test_y(): assert False\n"),
    ])
    fake_client_factory([
        PLAN, TESTER_WRITE, TESTER_DONE, tester_retry_write, TESTER_DONE,
        CODER_WRITE_OTHER, CODER_DONE, APPROVE,
    ])

    def check(wt_path):
        # green until the tester's second file exists, then red until NOTES.txt
        wt = Path(wt_path)
        if not (wt / "tests" / "test_y.py").exists():
            return True, "green"
        ok = (wt / "NOTES.txt").exists()
        return ok, "ok" if ok else "red"

    orch = TeamOrchestrator(Config(), str(repo), approve=lambda *_: Approval.GRANTED,
                            check=check, check_label="q")
    result = orch.run("add x", branch="retryred")
    assert result.status == "merged", result.summary
    assert result.red_green == "red->green"
    roles = [r for r, _ in result.role_seconds]
    assert roles == ["planner", "tester", "tester-retry", "coder", "reviewer"]
    assert [c["role"] for c in result.tester_calls] == ["tester", "tester-retry"]


def test_tester_writes_nothing_reprompt_then_fail_tester_no_tests(repo, fake_client_factory):
    tester_reads = Reply(content="t", tool_calls=[call("list_files", path=".")])
    client = fake_client_factory([PLAN, tester_reads, TESTER_DONE, TESTER_DONE])
    result = _orch(repo).run("add x", branch="notests")
    assert result.status == "tester_no_tests"
    assert [r for r, _ in result.role_seconds] == ["planner", "tester", "tester-retry"]
    assert "not written any test file" in client.seen[-1][-1]["content"]
    assert result.tester_calls == [
        {"role": "tester", "name": "list_files", "args": "path=.", "status": "ok"}]


def test_tester_writes_nothing_then_writes_on_reprompt_proceeds(repo, fake_client_factory):
    fake_client_factory([PLAN, TESTER_DONE, TESTER_WRITE, TESTER_DONE,
                         CODER_WRITE_MARKER, CODER_DONE, APPROVE])
    result = _orch(repo).run("add x", branch="latetests")
    assert result.status == "merged", result.summary
    assert result.red_green == "red->green"
    assert result.tester_calls[0]["role"] == "tester-retry"
    assert result.tester_calls[0]["name"] == "write_file"
    assert "<" in result.tester_calls[0]["args"] or "content=" in result.tester_calls[0]["args"]


def test_tester_calls_recorded_with_status(repo, fake_client_factory):
    tester_mixed = Reply(content="t", tool_calls=[
        call("read_file", path="missing.py"),
        call("write_file", path="tests/test_x.py", content="def test_x(): assert False\n"),
    ])
    fake_client_factory([PLAN, tester_mixed, TESTER_DONE, CODER_WRITE_MARKER,
                         CODER_DONE, APPROVE])
    result = _orch(repo).run("add x", branch="calls")
    assert [(c["name"], c["status"]) for c in result.tester_calls] == [
        ("read_file", "error"), ("write_file", "ok")]
    assert "path=tests/test_x.py" in result.tester_calls[1]["args"]


def test_tester_that_implements_the_feature_is_reverted(repo, fake_client_factory):
    # Live run 2026-10-02: the tester wrote the implementation alongside its
    # tests, so the red gate passed and the coder had nothing to do.
    tester_overreach = Reply(content="t", tool_calls=[
        call("write_file", path="tests/test_x.py", content="def test_x(): assert False\n"),
        call("write_file", path=".team_test_passed", content="ok\n"),
        call("write_file", path="impl.py", content="x = 2\n"),
    ])
    fake_client_factory([
        PLAN, tester_overreach, TESTER_DONE, CODER_WRITE_MARKER, CODER_DONE, APPROVE,
    ])
    result = _orch(repo).run("add x", branch="overreach")
    assert sorted(result.tester_reverted) == [".team_test_passed", "impl.py"]
    assert result.red_green == "red->green"
    assert result.status == "merged"
    assert not (repo / "impl.py").exists()
    assert (repo / "tests" / "test_x.py").exists()


def test_confine_to_tests_restores_a_modified_tracked_file(repo):
    from wilbur.team import confine_to_tests
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "app")
    (repo / "app.py").write_text("x = 2\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test(): pass\n")
    assert confine_to_tests(str(repo)) == ["app.py"]
    assert (repo / "app.py").read_text() == "x = 1\n"
    assert (repo / "tests" / "test_app.py").exists()


def test_is_test_path():
    from wilbur.team import is_test_path
    for p in ("tests/test_a.py", "test_a.py", "pkg/a_test.go", "web/a.test.ts",
              "src/__tests__/a.js", "conftest.py"):
        assert is_test_path(p), p
    for p in ("a.py", "cli.py", "testing.md", "src/contest.py", ".team_test_passed"):
        assert not is_test_path(p), p


def test_explicit_check_replaces_project_detection(repo, fake_client_factory):
    fake_client_factory([
        PLAN, TESTER_WRITE, TESTER_DONE, CODER_WRITE_OTHER, CODER_DONE, APPROVE,
    ])
    seen = []

    def check(wt_path):
        seen.append(wt_path)
        ok = (Path(wt_path) / "NOTES.txt").exists()
        return ok, "notes" if ok else "no notes"

    orch = TeamOrchestrator(Config(), str(repo), approve=lambda *_: Approval.GRANTED,
                            check=check, check_label="queue")
    result = orch.run("add x", branch="explicit")
    # the Makefile marker is never written, so only the explicit check can pass
    assert result.status == "merged", result.summary
    assert result.red_green == "red->green"
    assert all(".wilbur-worktrees" in p for p in seen)
