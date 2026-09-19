"""The harness runs the project's own check so the model does not have to
remember to. It must never invent one, and must never call an unrunnable check
a pass."""
from __future__ import annotations

import json

from wilbur import verify


def test_no_check_detected_in_an_empty_directory(tmp_path):
    """Silence is correct here. Inventing a command to run against someone's
    repository is worse than doing nothing."""
    assert verify.detect(str(tmp_path)) is None


def test_detects_this_projects_own_pytest_suite():
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    check = verify.detect(str(repo))
    assert check is not None and "pytest" in check.label


def test_makefile_wins_over_pytest(tmp_path):
    """A repo whose author wired up a Makefile target meant that target."""
    (tmp_path / "Makefile").write_text("check:\n\techo hi\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("")
    check = verify.detect(str(tmp_path))
    assert check is not None and check.label == "make check"


def test_package_json_script_is_used(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    monkeypatch.setattr(verify.shutil, "which", lambda n: "/usr/bin/npm")
    check = verify.detect(str(tmp_path))
    assert check is not None and check.label == "npm run test"


def test_malformed_package_json_does_not_raise(tmp_path):
    (tmp_path / "package.json").write_text("{not json")
    verify.detect(str(tmp_path))          # must not raise


def test_a_passing_check_reports_passed(tmp_path):
    check = verify.Check("true", ["true"])
    passed, out = verify.run(check, str(tmp_path))
    assert passed is True
    assert "PASSED" in verify.summarise(check, passed, out)


def test_a_failing_check_reports_failed_with_output(tmp_path):
    check = verify.Check("false", ["sh", "-c", "echo boom >&2; exit 1"])
    passed, out = verify.run(check, str(tmp_path))
    assert passed is False
    summary = verify.summarise(check, passed, out)
    assert "FAILED" in summary and "boom" in summary


def test_an_unrunnable_check_is_unknown_not_failed(tmp_path):
    """UNKNOWN and FAILED are different facts. Reporting a check that could not
    execute as a failure would train the model to 'fix' working code."""
    check = verify.Check("missing", ["definitely-not-a-real-binary-xyz"])
    passed, out = verify.run(check, str(tmp_path))
    assert passed is None
    assert "UNKNOWN" in verify.summarise(check, passed, out)


def test_a_hanging_check_times_out_as_unknown(tmp_path):
    check = verify.Check("hang", ["sleep", "30"])
    passed, out = verify.run(check, str(tmp_path), timeout=1)
    assert passed is None and "timed out" in out
