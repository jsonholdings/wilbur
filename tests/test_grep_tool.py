"""End-to-end behaviour of the Grep tool against a real tmp_path tree.

Exercises the live subprocess path (rg when installed, the grep fallback
otherwise) rather than the argv construction pinned in test_search_cmd.py:
a match is found, a miss is reported cleanly, and an invalid regex comes
back as an error result instead of raising.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur.config import Config
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.search import Grep


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(cwd=str(tmp_path), config=Config(), read_files={},
                       approve=lambda *_: Approval.GRANTED)


def test_finds_a_line_by_regex(tmp_path):
    (tmp_path / "notes.txt").write_text("alpha\nneedle in the haystack\nbeta\n")
    result = Grep().run({"pattern": r"needle.*haystack", "path": str(tmp_path)},
                        _ctx(tmp_path))
    assert not result.is_error
    assert "needle in the haystack" in result.output
    # file and line number are reported alongside the match
    assert "notes.txt" in result.output
    assert "2:" in result.output


def test_no_match_is_clean_not_an_error(tmp_path):
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n")
    result = Grep().run({"pattern": "needle", "path": str(tmp_path)},
                        _ctx(tmp_path))
    assert not result.is_error
    assert "No matches" in result.output
    assert "needle" in result.output


def test_invalid_regex_is_an_error_result(tmp_path):
    (tmp_path / "notes.txt").write_text("alpha\n")
    result = Grep().run({"pattern": "[", "path": str(tmp_path)},
                        _ctx(tmp_path))
    assert result.is_error
    assert result.output  # the backend's diagnostic, not an empty string
