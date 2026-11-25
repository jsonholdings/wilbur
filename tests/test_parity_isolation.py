"""Parity gap G2 (docs/PARITY-ANALYSIS.md; bench-side G1/G3 tests are in
tests/test_bench_isolation.py).

G2: `--approve auto-edit` and `auto_approve` grant edits inside the session's
working directory only; an edit outside it is asked about (Claude Code's
acceptEdits behaviour)."""
import io
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur.config import Config  # noqa: E402
from wilbur.repl import Repl  # noqa: E402
from wilbur.tools import build_registry  # noqa: E402
from wilbur.tools.base import OUTSIDE_CWD, Approval, ToolContext, outside_cwd  # noqa: E402

# ---------------------------------------------------------------- G2 approval


def test_outside_cwd(tmp_path):
    assert outside_cwd(tmp_path / "a" / "b.py", str(tmp_path)) is False
    assert outside_cwd(tmp_path.parent / "x.py", str(tmp_path)) is True


def _write(tmp_path: Path, target: Path) -> list[str]:
    details: list[str] = []

    def approve(tool, detail):
        details.append(detail)
        return Approval.GRANTED

    ctx = ToolContext(cwd=str(tmp_path / "cwd"), config=Config(), read_files={},
                      approve=approve)
    (tmp_path / "cwd").mkdir(exist_ok=True)
    tools = build_registry()
    result = tools["write_file"].run({"path": str(target), "content": "x\n"}, ctx)
    assert not result.is_error, result.output
    return details


def test_write_outside_cwd_is_flagged_in_the_approval(tmp_path):
    assert _write(tmp_path, tmp_path / "elsewhere.py")[0].startswith(OUTSIDE_CWD)


def test_write_inside_cwd_is_not_flagged(tmp_path):
    """Control for the flag: an in-cwd write carries no OUTSIDE marker."""
    assert not _write(tmp_path, tmp_path / "cwd" / "a.py")[0].startswith(OUTSIDE_CWD)


def _fake_repl(mode: str, auto_approve=()):
    return SimpleNamespace(
        approve_mode=mode, config=SimpleNamespace(auto_approve=list(auto_approve)),
        _spinner=None, _awaiting_approval=threading.Event(), _pump_thread=None,
        _ptk_thread=None, _turn_refused=False, _use_ptk=False)


@pytest.mark.parametrize("mode,auto", [("auto-edit", ()), ("ask", ("write_file",))])
def test_auto_edit_asks_for_a_write_outside_cwd(monkeypatch, capsys, mode, auto):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # not a tty: asking -> UNAVAILABLE
    repl = _fake_repl(mode, auto)
    assert Repl._approve(repl, "write_file", "write 2 bytes to /a.py") is Approval.GRANTED
    outcome = Repl._approve(repl, "write_file", OUTSIDE_CWD + "write 2 bytes to /a.py")
    assert outcome is Approval.UNAVAILABLE


def test_approve_all_still_grants_outside_cwd():
    repl = _fake_repl("all")
    assert Repl._approve(repl, "edit_file", OUTSIDE_CWD + "edit /a.py") is Approval.GRANTED
