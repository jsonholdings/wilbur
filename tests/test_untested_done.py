"""Parity G4 (bench 12_json_deep_merge): a reply that claims the fix is done,
verified or passing, after code was written in the same run with no test run
since, gets one [harness] nudge to run the tests before the answer stands."""
from wilbur.fake_backend import Reply

from conftest import call, say


def _write(path="merge.py", content="def merge(a, b):\n    return {**a, **b}\n"):
    return Reply(content="", tool_calls=[call("write_file", path=path, content=content)])


def _users(client):
    return [m.get("content", "") for m in client.seen[-1] if m.get("role") == "user"]


def test_fixed_claim_without_test_is_nudged_once(make_agent):
    agent, client = make_agent([_write(), say("Fixed and verified."),
                                say("Fixed and verified.")])
    assert agent.run("fix merge") == "Fixed and verified."
    assert len(client.seen) == 3
    nudges = [u for u in _users(client) if "[harness]" in u and "run the tests" in u]
    assert len(nudges) == 1


def test_project_tests_run_by_harness_after_write_means_no_nudge(make_agent, tmp_path):
    """With project tests present the harness runs them after the write
    (_verify_after_write), so the claim rests on a real test run: no nudge."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_merge.py").write_text("def test_x():\n    pass\n")
    agent, client = make_agent([_write(), say("The bug is fixed.")])
    assert agent.run("fix merge") == "The bug is fixed."
    assert len(client.seen) == 2


def test_sibling_tests_found(tmp_path):
    from wilbur.doneclaim import sibling_tests
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "test_merge.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_util.py").write_text("")
    assert sibling_tests(str(tmp_path), ["pkg/merge.py", "util.py", "other.py"]) == [
        "pkg/test_merge.py", "tests/test_util.py"]


def test_nudge_names_sibling_tests(tmp_path):
    from wilbur.doneclaim import untested_done_nudge
    (tmp_path / "test_merge.py").write_text("")
    nudge = untested_done_nudge("Fixed.", ["merge.py"], str(tmp_path))
    assert nudge and "test_merge.py" in nudge and "run_tests" in nudge


def test_control_test_run_after_write_is_not_nudged(make_agent):
    """Control: run a test command after the write, then claim -- no nudge."""
    agent, client = make_agent([
        _write(),
        Reply(content="", tool_calls=[call("run_bash", command="python -m pytest -q || true")]),
        say("Fixed and verified."),
    ])
    assert agent.run("fix merge") == "Fixed and verified."
    assert len(client.seen) == 3
    assert not any("run the tests" in u for u in _users(client))


def test_control_run_tests_after_write_is_not_nudged(make_agent):
    """Control: run_tests after the write clears the untested list (verifier gap, 2.55.0)."""
    agent, client = make_agent([
        _write(),
        Reply(content="", tool_calls=[call("run_tests", command="true")]),
        say("Fixed and verified."),
    ])
    assert agent.run("fix merge") == "Fixed and verified."
    assert len(client.seen) == 3
    assert not any("run the tests" in u for u in _users(client))


def test_control_no_write_no_nudge(make_agent):
    agent, client = make_agent([say("It is fixed.")])
    assert agent.run("is it fixed?") == "It is fixed."
    assert len(client.seen) == 1


def test_control_docs_write_no_nudge(make_agent):
    agent, client = make_agent([_write(path="NOTES.md", content="x\n"), say("Fixed the typo.")])
    assert agent.run("fix typo") == "Fixed the typo."
    assert len(client.seen) == 2


def test_control_plain_reply_without_claim_no_nudge(make_agent):
    agent, client = make_agent([_write(), say("done")])
    assert agent.run("write it") == "done"
    assert len(client.seen) == 2


def test_off_switch(make_agent):
    agent, client = make_agent([_write(), say("Fixed and verified.")],
                               verify_done_claims=False)
    assert agent.run("fix merge") == "Fixed and verified."
    assert len(client.seen) == 2
