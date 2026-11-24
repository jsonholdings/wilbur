"""2.52.1: run_tests `cwd`, the DID NOT RUN header, and the timeout retry hint.

Seen 2026-11-24: the model ran `./venv/bin/python -m pytest` through run_tests from the session
cwd (home), so the relative venv resolved to a different python ("No module named pytest"), and
the summary said "0 passed, 0 failed" as if the suite were clean. Separately it resent one
run_bash at timeout=60 three times until the tool-failure breaker stopped it.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools import contained
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.shell import Bash
from wilbur.tools.test_runner import RunTests, _format_custom_summary


def _ctx(cwd):
    return ToolContext(cwd=str(cwd), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "marker.txt").write_text("here\n")
    return proj


def test_cwd_arg_runs_command_in_that_directory(tmp_path, project):
    out = RunTests().run({"command": "cat marker.txt && echo '1 test passed'", "cwd": str(project)},
                         _ctx(tmp_path))
    assert not out.is_error, out.output
    assert "1 passed" in out.output


def test_relative_cwd_resolves_from_session_cwd(tmp_path, project):
    out = RunTests().run({"command": "cat marker.txt && echo '1 test passed'", "cwd": "proj"},
                         _ctx(tmp_path))
    assert not out.is_error, out.output


def test_control_without_cwd_the_relative_path_misses(tmp_path, project):
    # control: proves the cwd test above can fail -- same command from the session cwd breaks
    out = RunTests().run({"command": "cat marker.txt"}, _ctx(tmp_path))
    assert out.is_error


def test_missing_cwd_is_refused(tmp_path):
    out = RunTests().run({"command": "true", "cwd": str(tmp_path / "nope")}, _ctx(tmp_path))
    assert out.is_error and "not a directory" in out.output


def test_command_that_runs_no_tests_says_did_not_run(tmp_path):
    out = RunTests().run({"command": "echo 'No module named pytest' >&2; exit 1"}, _ctx(tmp_path))
    assert out.is_error
    assert "DID NOT RUN" in out.output
    assert str(tmp_path) in out.output
    assert "0 passed, 0 failed" not in out.output


def test_control_real_failure_counts_still_reported():
    # control: a run that did report counts keeps the normal header
    text = _format_custom_summary("custom", "3 tests passed\n1 test failed", 1, "/x")
    assert text.startswith("tests: 3 passed, 1 failed")
    assert "DID NOT RUN" not in text


def test_clean_exit_with_no_counts_is_not_flagged():
    text = _format_custom_summary("custom", "ok", 0, "/x")
    assert "DID NOT RUN" not in text


def test_bash_timeout_carries_retry_hint(tmp_path):
    out = Bash().run({"command": "sleep 5", "timeout": 1}, _ctx(tmp_path))
    assert out.is_error
    assert "timed out after 1s" in out.output
    assert "raise `timeout`" in out.output


def test_timeout_hint_names_the_value():
    assert "it was 60s" in contained.timeout_hint(60)
