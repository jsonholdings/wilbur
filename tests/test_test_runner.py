"""RunTests tool: detection, JUnit parsing, approval gating.

Control per this repo's convention (see test_repair_and_retry.py): a
positive case (passing project -> genuinely 0 failed) is paired with a
negative case (failing project -> genuinely >=1 failed, right test name),
so a vacuously "clean" parser can't pass silently.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.test_runner import RunTests


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


def test_passing_project_reports_zero_failures(tmp_path, ctx):
    (tmp_path / "test_ok.py").write_text(
        "def test_pass():\n    assert 1 == 1\n"
    )
    out = RunTests().run({}, ctx)
    assert not out.is_error
    assert "0 failed" in out.output
    assert "1 total, 1 passed" in out.output


def test_failing_project_reports_failure_with_location(tmp_path, ctx):
    (tmp_path / "test_fail.py").write_text(
        "def test_bad():\n    assert 1 == 2\n"
    )
    out = RunTests().run({}, ctx)
    assert out.is_error
    assert "1 failed" in out.output
    assert "test_fail.py" in out.output
    assert "test_bad" in out.output
    assert "assert 1 == 2" in out.output


def test_no_detected_command_requires_explicit_override(tmp_path, ctx):
    out = RunTests().run({}, ctx)
    assert out.is_error
    assert "command" in out.output.lower()


def test_explicit_command_override_used_verbatim(tmp_path, ctx):
    (tmp_path / "test_fail.py").write_text(
        "def test_bad():\n    assert 1 == 2\n"
    )
    out = RunTests().run({"command": "echo custom-ran"}, ctx)
    assert not out.is_error
    assert "custom-ran" in out.output


def test_approval_denied_does_not_run(tmp_path, ctx):
    (tmp_path / "test_ok.py").write_text(
        "def test_pass():\n    assert 1 == 1\n"
    )
    ctx.approve = lambda tool, detail: Approval.DENIED
    out = RunTests().run({}, ctx)
    assert out.is_error
    assert "denied" in out.output.lower()
