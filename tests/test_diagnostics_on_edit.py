"""BACKLOG tool-diagnostics-on-edit: checker findings appended to edit/write results."""
import shutil
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools import diagnostics
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.files import EditFile, WriteFile


def _script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / "bin" / name
    script.parent.mkdir(exist_ok=True)
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def work(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


def _ctx(proj: Path, **cfg) -> ToolContext:
    config = Config()
    for key, value in cfg.items():
        setattr(config, key, value)
    return ToolContext(cwd=str(proj), config=config, read_files={},
                       approve=lambda tool, detail: Approval.GRANTED)


def _clean_python_cfg(tmp_path: Path) -> dict:
    ok = _script(tmp_path, "ok", "exit 0")
    return {"diagnostics_commands": {"ruff": [ok], "pyright": [ok]}}


def test_planted_python_error_appears_in_edit_result(work, tmp_path):
    f = work / "m.py"
    f.write_text("def f():\n    return 1\n")
    ctx = _ctx(work, **_clean_python_cfg(tmp_path))
    ctx.read_files[str(f)] = f.stat().st_mtime
    res = EditFile().run({"path": "m.py", "old_string": "return 1", "new_string": "return (1"}, ctx)
    assert not res.is_error
    assert res.output.startswith(f"Edited {f} (1 replacement).")
    assert "[diagnostics: m.py: 1 finding(s)]" in res.output
    assert "python-syntax: m.py:2:" in res.output


def test_external_checker_findings_are_appended_with_relative_path(work, tmp_path):
    ruff = _script(tmp_path, "fakeruff", 'echo "$1:1:1: F401 os imported but unused"; exit 1')
    ok = _script(tmp_path, "ok", "exit 0")
    ctx = _ctx(work, diagnostics_commands={"ruff": [ruff], "pyright": [ok]})
    res = WriteFile().run({"path": "n.py", "content": "import os\n"}, ctx)
    assert "ruff: n.py:1:1: F401 os imported but unused" in res.output
    assert str(work / "n.py") + ":1:1" not in res.output


def test_clean_file_reports_nothing(work, tmp_path):
    ctx = _ctx(work, **_clean_python_cfg(tmp_path))
    res = WriteFile().run({"path": "c.py", "content": "x = 1\n"}, ctx)
    assert res.output == f"Created {work / 'c.py'} (1 lines)."


def test_missing_checker_gives_explicit_skipped_line(work, tmp_path):
    ok = _script(tmp_path, "ok", "exit 0")
    missing = str(tmp_path / "bin" / "no-such-ruff")
    ctx = _ctx(work, diagnostics_commands={"ruff": [missing], "pyright": [ok]})
    res = WriteFile().run({"path": "c.py", "content": "x = 1\n"}, ctx)
    assert "[diagnostics: c.py: no findings]" in res.output
    assert res.output.rstrip().endswith("skipped (not installed): ruff")


def test_timeout_holds_and_kills_the_process_group(work, tmp_path):
    # The grandchild keeps stdout open: without the group kill, communicate()
    # would block until it exits (30 s).
    slow = _script(tmp_path, "slow", "sleep 30 &\nsleep 30")
    ok = _script(tmp_path, "ok", "exit 0")
    ctx = _ctx(work, diagnostics_commands={"ruff": [slow], "pyright": [ok]},
               diagnostics_timeout_s=0.5)
    started = time.monotonic()
    res = WriteFile().run({"path": "c.py", "content": "x = 1\n"}, ctx)
    assert time.monotonic() - started < 5
    assert "ruff: timed out after 0.5s (no result)" in res.output


def test_switch_off_is_byte_identical_to_old_result(work, tmp_path):
    body = "def f(:\n"  # a syntax error that would be reported if on
    on = _ctx(work, **_clean_python_cfg(tmp_path))
    off = _ctx(work, diagnostics_on_edit=False)
    res_on = WriteFile().run({"path": "a.py", "content": body}, on)
    res_off = WriteFile().run({"path": "b.py", "content": body}, off)
    assert "python-syntax" in res_on.output          # control: on does report
    assert res_off.output == f"Created {work / 'b.py'} (1 lines)."
    f = work / "b.py"
    off.read_files[str(f)] = f.stat().st_mtime
    res = EditFile().run({"path": "b.py", "old_string": "f(:", "new_string": "g(:"}, off)
    assert res.output == f"Edited {f} (1 replacement)."


def test_output_is_capped(work, tmp_path):
    noisy = _script(tmp_path, "noisy",
                    'i=0; while [ $i -lt 200 ]; do echo "$1:$i:1: E1 bad"; i=$((i+1)); done; exit 1')
    ok = _script(tmp_path, "ok", "exit 0")
    ctx = _ctx(work, diagnostics_commands={"ruff": [noisy], "pyright": [ok]},
               diagnostics_max_lines=5)
    res = WriteFile().run({"path": "c.py", "content": "x = 1\n"}, ctx)
    assert res.output.count("ruff: c.py:") == 5
    assert "... 195 more finding(s) not shown" in res.output


def test_pyright_keeps_only_error_and_warning_lines(work, tmp_path):
    pyright = _script(tmp_path, "fakepyright",
                      'echo "$1"; echo "  $1:3:5 - error: \\"y\\" is not defined"; '
                      'echo "1 error, 0 warnings, 0 informations"; exit 1')
    ok = _script(tmp_path, "ok", "exit 0")
    ctx = _ctx(work, diagnostics_commands={"ruff": [ok], "pyright": [pyright]})
    res = WriteFile().run({"path": "c.py", "content": "x = 1\n"}, ctx)
    assert 'pyright: c.py:3:5 - error: "y" is not defined' in res.output
    assert "informations" not in res.output


def test_checker_crash_is_reported_not_silent(work, tmp_path):
    bad = _script(tmp_path, "bad", 'echo "config parse failure"; exit 2')
    ok = _script(tmp_path, "ok", "exit 0")
    ctx = _ctx(work, diagnostics_commands={"ruff": [bad], "pyright": [ok]})
    res = WriteFile().run({"path": "c.py", "content": "x = 1\n"}, ctx)
    assert "ruff: could not run (exit 2; config parse failure)" in res.output


def test_routing_by_suffix_and_shebang():
    assert diagnostics.checkers_for(Path("a.py")) == ["python-syntax", "ruff", "pyright"]
    assert diagnostics.checkers_for(Path("a.sh")) == ["shellcheck"]
    assert diagnostics.checkers_for(Path("a.c")) == ["c"]
    assert diagnostics.checkers_for(Path("a.cpp")) == ["c++"]
    assert diagnostics.checkers_for(Path("a.gd")) == ["gdlint"]
    assert diagnostics.checkers_for(Path("run"), "#!/usr/bin/env bash\n") == ["shellcheck"]
    assert diagnostics.checkers_for(Path("a.txt"), "hello") == []


def test_unchecked_file_type_is_unchanged(work):
    ctx = _ctx(work)
    res = WriteFile().run({"path": "notes.txt", "content": "hi\n"}, ctx)
    assert res.output == f"Created {work / 'notes.txt'} (1 lines)."


def test_disabled_checker_is_not_run_or_listed(work, tmp_path):
    ctx = _ctx(work, diagnostics_disabled=["ruff", "pyright"])
    res = WriteFile().run({"path": "c.py", "content": "x = 1\n"}, ctx)
    assert res.output == f"Created {work / 'c.py'} (1 lines)."


@pytest.mark.skipif(bool(not (shutil.which("cc") or shutil.which("gcc")) or shutil.which("clang-tidy")),
                    reason="needs a C compiler and no clang-tidy")
def test_real_c_compile_reports_syntax_error(work):
    ctx = _ctx(work)
    res = WriteFile().run({"path": "t.c", "content": "int main(void) { return 0 }\n"}, ctx)
    assert "[diagnostics: t.c:" in res.output
    assert "c: t.c:1:" in res.output and "error" in res.output


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="needs shellcheck")
def test_real_shellcheck_reports_finding(work):
    ctx = _ctx(work)
    res = WriteFile().run({"path": "t.sh", "content": "#!/bin/sh\necho $1\n"}, ctx)
    assert "shellcheck: t.sh:2:" in res.output
