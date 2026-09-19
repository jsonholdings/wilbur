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
