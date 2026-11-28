"""Test-impact selection: the tests covering a changed module run first, the
full suite still runs, and `test_impact: False` restores the old behaviour."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur import impact
from wilbur.config import Config
from wilbur.tools import test_runner
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.test_runner import RunTests


def _project(root: Path, calc_body: str = "def add(a, b):\n    return a + b\n") -> None:
    pkg = root / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "calc.py").write_text(calc_body)
    (pkg / "other.py").write_text("X = 1\n")
    (pkg / "service.py").write_text("from .calc import add\n\ndef total(xs):\n    return add(*xs)\n")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(
        "from app.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (tests / "test_service.py").write_text(
        "from app import service\n\ndef test_total():\n    assert service.total([1, 2]) == 3\n")
    (tests / "test_other.py").write_text(
        "import app.other\n\ndef test_x():\n    assert app.other.X == 1\n")
    (root / "pytest.ini").write_text("[pytest]\npythonpath = .\n")


def test_edited_module_selects_its_direct_and_transitive_tests(tmp_path):
    _project(tmp_path)
    picked = impact.impacted_tests(tmp_path, [tmp_path / "app" / "calc.py"])
    names = [p.name for p in picked]
    assert names == ["test_calc.py", "test_service.py"]


def test_unrelated_module_selects_none(tmp_path):
    _project(tmp_path)
    (tmp_path / "notes.py").write_text("import os\n")
    assert impact.impacted_tests(tmp_path, [tmp_path / "notes.py"]) == []
    assert impact.impacted_tests(tmp_path, [tmp_path / "README.md"]) == []


def test_changed_test_file_selects_itself(tmp_path):
    _project(tmp_path)
    picked = impact.impacted_tests(tmp_path, [tmp_path / "tests" / "test_other.py"])
    assert [p.name for p in picked] == ["test_other.py"]


def test_syntax_error_file_does_not_break_selection(tmp_path):
    _project(tmp_path)
    (tmp_path / "app" / "broken.py").write_text("def (:\n")
    assert [p.name for p in impact.impacted_tests(tmp_path, [tmp_path / "app" / "other.py"])] \
        == ["test_other.py"]


def _ctx(tmp_path, **cfg):
    config = Config()
    for k, v in cfg.items():
        setattr(config, k, v)
    seen = []

    def approve(tool, detail):
        seen.append(detail)
        return Approval.GRANTED
    return ToolContext(cwd=str(tmp_path), config=config, read_files={}, approve=approve), seen


def _record_shell(monkeypatch):
    calls = []
    real = test_runner.contained.shell

    def shell(command, cwd, timeout, memory_max):
        calls.append(command)
        return real(command, cwd, timeout, memory_max)
    monkeypatch.setattr(test_runner.contained, "shell", shell)
    return calls


def test_impacted_tests_run_first_then_full_suite(tmp_path, monkeypatch):
    _project(tmp_path)
    calls = _record_shell(monkeypatch)
    ctx, seen = _ctx(tmp_path)
    out = RunTests().run({"changed": ["app/calc.py"]}, ctx)
    assert not out.is_error, out.output
    assert len(calls) == 2
    assert "tests/test_calc.py tests/test_service.py" in calls[0]
    assert "test_other" not in calls[0]
    assert "pytest -q  --junitxml" in calls[1] or "pytest -q --junitxml" in calls[1]
    assert "passed first; full suite follows" in out.output
    assert "3 total, 3 passed" in out.output
    assert len(seen) == 1 and "tests/test_calc.py" in seen[0]


def test_impacted_failure_stops_before_the_full_suite(tmp_path, monkeypatch):
    _project(tmp_path, calc_body="def add(a, b):\n    return a - b\n")
    calls = _record_shell(monkeypatch)
    ctx, _ = _ctx(tmp_path)
    out = RunTests().run({"changed": ["app/calc.py"]}, ctx)
    assert out.is_error
    assert len(calls) == 1
    assert "FAILED (full suite not run yet" in out.output
    assert "test_add" in out.output


def test_unrelated_change_runs_only_the_full_suite(tmp_path, monkeypatch):
    _project(tmp_path)
    (tmp_path / "notes.py").write_text("import os\n")
    calls = _record_shell(monkeypatch)
    ctx, _ = _ctx(tmp_path)
    out = RunTests().run({"changed": ["notes.py"]}, ctx)
    assert len(calls) == 1 and "impact:" not in out.output
    assert "3 total, 3 passed" in out.output


@pytest.mark.parametrize("switch", [True, False])
def test_switch_off_is_the_old_behaviour(tmp_path, monkeypatch, switch):
    _project(tmp_path)
    calls = _record_shell(monkeypatch)
    ctx, seen = _ctx(tmp_path, test_impact=switch)
    out = RunTests().run({"changed": ["app/calc.py"]}, ctx)
    assert not out.is_error
    if switch:  # the control: the same call with the switch on does select
        assert len(calls) == 2 and "impact:" in out.output
    else:
        assert len(calls) == 1 and "impact:" not in out.output
        assert "&&" not in seen[0]


def test_changed_files_from_git(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _project(tmp_path)
    found = {p.name for p in impact.changed_files(tmp_path)}
    assert {"calc.py", "test_calc.py"} <= found


def test_changed_files_outside_git_is_empty(tmp_path):
    (tmp_path / "a.py").write_text("")
    assert impact.changed_files(tmp_path) == []
