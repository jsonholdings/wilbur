"""BACKLOG tool-code-intel (Python first): code_intel symbols, definition and
references from the stdlib ast, with controls for misses and broken files."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools import build_registry
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.code_intel import CodeIntel

ENGINE = '''"""engine"""
LIMIT = 3


class Engine:
    def run(self, steps):
        return self.step(steps)

    async def step(self, n):
        return n * LIMIT


def run(engine):
    return engine.run(LIMIT)
'''

CALLER = '''from engine import Engine, run as go

def main():
    e = Engine()
    text = "run"  # run in a comment is not a reference
    return go(e), e.run(1)
'''


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "engine.py").write_text(ENGINE)
    (root / "pkg" / "caller.py").write_text(CALLER)
    (root / "broken.py").write_text("def oops(:\n")
    (root / ".venv").mkdir()
    (root / ".venv" / "dep.py").write_text("def run():\n    pass\n")
    return root


def _ctx(root: Path) -> ToolContext:
    return ToolContext(cwd=str(root), config=Config(), read_files={},
                       approve=lambda *_: Approval.GRANTED)


def _run(root, **args):
    return CodeIntel().run(args, _ctx(root))


def test_symbols_outline_with_spans(proj):
    out = _run(proj, action="symbols", path="engine.py").output.splitlines()
    assert out == ["5-10 class Engine", "  6-7 method run", "  9-10 async method step",
                   "13-14 def run"]


def test_definition_bare_and_qualified(proj):
    bare = _run(proj, action="definition", name="run").output
    assert "engine.py:6: method Engine.run" in bare
    assert "engine.py:13: def run" in bare
    assert ".venv" not in bare  # SKIP_DIRS honoured
    qual = _run(proj, action="definition", name="Engine.run").output
    assert qual.splitlines()[0] == "engine.py:6: method Engine.run"
    assert "def run" not in qual.split("skipped")[0]
    assert "engine.py:2: assignment LIMIT" in _run(proj, action="definition", name="LIMIT").output


def test_references_skip_strings_and_comments(proj):
    out = _run(proj, action="references", name="run", path="pkg").output
    lines = [l for l in out.splitlines() if l.startswith("pkg/")]
    assert [l.split(":")[1] for l in lines] == ["1", "6"]  # import alias + e.run(1)
    assert all(":5:" not in l for l in lines)


def test_broken_file_named_as_skipped(proj):
    out = _run(proj, action="definition", name="oops").output
    assert "No definition found" in out and "skipped (does not parse): broken.py" in out
    sym = _run(proj, action="symbols", path="broken.py")
    assert sym.is_error and "does not parse" in sym.output


def test_controls_errors(proj):
    # Controls: a miss says so, bad inputs are errors, not empty successes.
    assert "No references found" in _run(proj, action="references", name="nothing_here").output
    assert _run(proj, action="definition").is_error
    assert _run(proj, action="symbols", path="missing.py").is_error
    assert _run(proj, action="bogus", name="x").is_error
    (proj / "notes.txt").write_text("def x(): pass\n")
    assert _run(proj, action="symbols", path="notes.txt").is_error


def test_result_cap(proj):
    (proj / "many.py").write_text("".join(f"x = {i}\n" for i in range(100)))
    out = _run(proj, action="references", name="x", path="many.py").output
    assert out.count("many.py:") == 60 and "40 more" in out


def test_off_by_default_and_switch_on():
    assert "code_intel" not in build_registry(config=Config())
    on = Config()
    on.code_intel_tool = True
    assert "code_intel" in build_registry(config=on)
    assert build_registry(config=on)["code_intel"].read_only
