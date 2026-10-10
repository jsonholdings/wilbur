"""Never accept a "done" claim without evidence (wilbur.doneclaim, 2.24.1).

Live run 2026-10-02 (qwen3:14b, calc task): the model wrote three files, said
"the tests have passed" without running pytest, left its plan at 0/4, and
answered six generic nudges with "All items are complete" until the stuck
detector stopped it. Driven through the real `Repl.run` against FakeClient.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur import doneclaim, keepgoing, verify
from wilbur.llm import Reply
from wilbur.state import RunState

from conftest import say, call
from test_keepgoing import _run

PROMPT = "Build calc/ops.py and tests/test_ops.py, run pytest until green."
PLAN = [{"task": "Create calc/ops.py with add", "status": "pending"},
        {"task": "Create tests/test_ops.py", "status": "pending"},
        {"task": "Run pytest and fix until tests pass", "status": "pending"}]
CLAIM = ("All three files have been created and the tests have passed "
         "successfully. No further action is needed.")
GOOD_OPS = "def add(a, b):\n    return a + b\n"
BAD_OPS = "def add(a, b):\n    return a - b\n"
TEST = ("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\n"
        "from calc.ops import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")


def _pytest_check(monkeypatch):
    monkeypatch.setattr(verify, "detect", lambda cwd: verify.Check(
        "pytest", [sys.executable, "-m", "pytest", "tests", "-q", "-x", "-p", "no:cacheprovider"])
        if (Path(cwd) / "tests").is_dir() else None)


def _plan() -> Reply:
    return Reply(content="", tool_calls=[call("todo_write", todos=PLAN)])


def _write(path: str, content: str) -> Reply:
    return Reply(content="", tool_calls=[call("write_file", path=path, content=content)])


def _nudge_texts(client) -> list[str]:
    last = client.seen[-1] if client.seen else []
    return [m["content"] for m in last if m.get("role") == "user"
            and m["content"].startswith("[harness] Your work is not verified")]


# -- unit ------------------------------------------------------------------- #

def test_claim_patterns():
    assert doneclaim.claims_done(CLAIM)
    assert doneclaim.claims_done("All items are complete. No further action is needed.")
    assert not doneclaim.claims_done("Item 1 done. Next, I'll create b.txt.")
    assert not doneclaim.claims_done("b.txt created.")
    assert doneclaim.claims_tests_pass("the tests have passed successfully")
    assert doneclaim.claims_tests_pass("All 4 tests pass.")
    assert not doneclaim.claims_tests_pass("I will write the tests next.")
    assert doneclaim.is_test_command(".venv/bin/python -m pytest -q")
    assert doneclaim.is_test_command("make test")
    assert not doneclaim.is_test_command("ls tests")


def test_named_files_from_plan():
    assert doneclaim.named_files([t["task"] for t in PLAN]) == ["calc/ops.py", "tests/test_ops.py"]
    assert doneclaim.named_files(["use argparse, e.g. like v1.2"]) == []


def test_check_reports_missing_files_and_no_check(tmp_path):
    st = RunState()
    st.set_plan(PLAN)
    v = doneclaim.check(str(tmp_path), st)
    assert v.passed is None and v.missing == ["calc/ops.py", "tests/test_ops.py"]
    assert not v.ok
    text = v.nudge()
    assert "calc/ops.py" in text and "[ ] Run pytest" in text


# -- through the REPL --------------------------------------------------------- #

def test_hallucinated_done_with_failing_tests_gets_a_specific_nudge(monkeypatch, tmp_path, capsys):
    _pytest_check(monkeypatch)
    replies = [_plan(), _write("calc/ops.py", BAD_OPS), _write("tests/test_ops.py", TEST),
               say(CLAIM),
               # after the specific nudge: fix it, then claim again
               _write("calc/ops.py", GOOD_OPS), say(CLAIM)]
    repl, client = _run(monkeypatch, tmp_path, replies, lines=(PROMPT,))
    nudges = _nudge_texts(client)
    assert len(nudges) == 1
    assert "FAILED: `pytest`" in nudges[0] and "assert" in nudges[0]
    assert "[ ] Create calc/ops.py with add" in nudges[0]
    assert "Do not claim completion without running the check" in nudges[0]
    assert all(m["content"] != keepgoing.NUDGE for m in client.seen[-1] if m.get("role") == "user")
    out = capsys.readouterr().out
    assert "not verified done" in out and "COMPLETE -- [verified] pytest passed" in out
    assert repl.state.progress == (3, 3)


def test_hallucinated_done_with_passing_tests_auto_completes(monkeypatch, tmp_path, capsys):
    _pytest_check(monkeypatch)
    replies = [_plan(), _write("calc/ops.py", GOOD_OPS), _write("tests/test_ops.py", TEST),
               say(CLAIM), _write("never.txt", "x")]
    repl, client = _run(monkeypatch, tmp_path, replies, lines=(PROMPT,))
    assert not (tmp_path / "never.txt").exists()          # no spinning after COMPLETE
    assert repl.state.progress == (3, 3)                    # todos ticked
    out = capsys.readouterr().out
    assert "COMPLETE -- [verified] pytest passed" in out
    assert "3 open plan item(s) marked done" in out
    assert "continuing" not in out


def test_a_real_done_is_left_alone(monkeypatch, tmp_path, capsys):
    _pytest_check(monkeypatch)
    done = [dict(t, status="completed") for t in PLAN]
    replies = [_plan(), _write("calc/ops.py", GOOD_OPS), _write("tests/test_ops.py", TEST),
               Reply(content="", tool_calls=[call("run_bash", command=f"{sys.executable} -m pytest -q tests")]),
               Reply(content="", tool_calls=[call("todo_write", todos=done)]),
               say("All three items are done; 1 test passed.")]
    repl, client = _run(monkeypatch, tmp_path, replies, lines=(PROMPT,), approve_mode="all")
    out = capsys.readouterr().out
    assert "unverified claim" not in out and "continuing" not in out
    assert "COMPLETE" not in out and _nudge_texts(client) == []
    assert repl.state.progress == (3, 3)


def test_claims_guard_flags_tests_pass_with_no_test_run(monkeypatch, tmp_path, capsys):
    # No plan, no write this turn (so the harness check did not run either):
    # "tests pass" is an unverified claim; the harness runs the check itself.
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "ops.py").write_text(BAD_OPS)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ops.py").write_text(TEST)
    _pytest_check(monkeypatch)
    replies = [say("I checked it: all tests pass."),
               Reply(content="", tool_calls=[call("read_file", path="calc/ops.py")]),
               _write("calc/ops.py", GOOD_OPS),
               say("Fixed; the tests pass now.")]
    repl, client = _run(monkeypatch, tmp_path, replies, lines=("is calc ok?",))
    out = capsys.readouterr().out
    assert "unverified claim" in out
    nudges = _nudge_texts(client)
    assert len(nudges) == 1 and "Unverified claim" in nudges[0] and "FAILED" in nudges[0]
    # The second claim follows a write, so the harness's own check ran: no flag.
    assert out.count("unverified claim:") == 1
    assert (tmp_path / "calc" / "ops.py").read_text() == GOOD_OPS


def test_repeated_identical_done_claims_still_trip_the_stuck_detector(monkeypatch, tmp_path, capsys):
    _pytest_check(monkeypatch)
    replies = [_plan(), _write("calc/ops.py", BAD_OPS), _write("tests/test_ops.py", TEST)] + \
              [say(CLAIM)] * 8

    def cfg(c):
        c.stuck_limit = 3
    _run(monkeypatch, tmp_path, replies, lines=(PROMPT,), configure=cfg)
    out = capsys.readouterr().out
    assert "no further progress for 3 turns -- stopping" in out
    # The first continue always counts as progress, so limit 3 = 4 continues.
    assert "continuing (5)" not in out


def test_run_tests_tool_counts_as_a_test_run(monkeypatch, tmp_path, capsys):
    # The 27b model verifies with the run_tests tool, not run_bash: that is
    # evidence, so "tests pass" afterwards is not an unverified claim.
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "ops.py").write_text(GOOD_OPS)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ops.py").write_text(TEST)
    _pytest_check(monkeypatch)
    replies = [Reply(content="", tool_calls=[call("run_tests", command=f"{sys.executable} -m pytest -q tests")]),
               say("Ran them: 1 test passed.")]
    _run(monkeypatch, tmp_path, replies, lines=("run the tests",), approve_mode="all")
    assert "unverified claim" not in capsys.readouterr().out
