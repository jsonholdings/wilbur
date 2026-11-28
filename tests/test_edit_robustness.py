"""edit-robustness (2.77.0): whitespace-tolerant edit_file anchors.

An exact match is applied byte-identically as before; with no exact match a
unique whitespace-tolerant match is used and re-indented; an ambiguous one is
refused with every location shown; a miss names the closest lines.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.files import EditFile, ReadFile


class Cfg:
    max_read_lines = 2000
    max_tool_output_chars = 100000
    max_tool_output_lines = 2000


def _edit(tmp_path: Path, content: bytes, old: str, new: str, **extra):
    target = tmp_path / "m.py"
    target.write_bytes(content)
    ctx = ToolContext(cwd=str(tmp_path), config=Cfg(), read_files={},
                      approve=lambda *a: Approval.GRANTED)
    ReadFile().run({"path": "m.py"}, ctx)
    result = EditFile().run({"path": "m.py", "old_string": old, "new_string": new, **extra}, ctx)
    return result, target.read_bytes()


SOURCE = b"def f():\n    if x:\n        return 1\n    return 2\n"


def test_exact_match_is_byte_identical_control(tmp_path):
    res, after = _edit(tmp_path, SOURCE, "        return 1", "        return 10")
    assert not res.is_error
    assert after == SOURCE.replace(b"return 1\n", b"return 10\n")
    assert "whitespace-tolerant" not in res.output
    assert res.diff["start"] == 3


def test_exact_replace_all_unchanged(tmp_path):
    res, after = _edit(tmp_path, b"a\nb\na\n", "a", "z", replace_all=True)
    assert not res.is_error and after == b"z\nb\nz\n"


def test_indent_shifted_anchor_matches_and_reindents(tmp_path):
    res, after = _edit(tmp_path, SOURCE, "if x:\n    return 1", "if y:\n    return 11")
    assert not res.is_error, res.output
    assert "whitespace-tolerant match at line 2" in res.output
    assert after == b"def f():\n    if y:\n        return 11\n    return 2\n"


def test_trailing_whitespace_anchor_matches(tmp_path):
    res, after = _edit(tmp_path, SOURCE, "    return 2   ", "    return 3")
    assert not res.is_error, res.output
    assert after == b"def f():\n    if x:\n        return 1\n    return 3\n"


def test_ambiguous_tolerant_match_is_refused_with_both_locations(tmp_path):
    content = b"def a():\n    pass\n\ndef b():\n  pass\n"
    res, after = _edit(tmp_path, content, " pass ", "    return")
    assert res.is_error and after == content
    assert "2 places" in res.output
    assert "lines 2-2" in res.output and "lines 5-5" in res.output


def test_miss_returns_closest_candidates_with_line_numbers(tmp_path):
    content = b"alpha = 1\nbeta = compute(alpha, 2)\ngamma = 3\n"
    res, after = _edit(tmp_path, content, "beta = compute(alpha, 3)", "beta = 0")
    assert res.is_error and after == content
    assert "Closest match" in res.output
    assert "lines 2-2" in res.output and "    2| beta = compute(alpha, 2)" in res.output


def test_miss_with_nothing_similar_has_no_candidates(tmp_path):
    res, _ = _edit(tmp_path, SOURCE, "zzzzqqqq wholly unrelated", "x")
    assert res.is_error and "Closest match" not in res.output


def test_crlf_file_lf_anchor_keeps_crlf(tmp_path):
    content = b"one\r\ntwo\r\nthree\r\n"
    res, after = _edit(tmp_path, content, "one\ntwo", "one\nTWO")
    assert not res.is_error, res.output
    assert after == b"one\r\nTWO\r\nthree\r\n"


def test_crlf_file_single_line_edit_keeps_other_line_endings(tmp_path):
    content = b"one\r\ntwo\r\n"
    res, after = _edit(tmp_path, content, "two", "2")
    assert not res.is_error and after == b"one\r\n2\r\n"


def test_crlf_file_tolerant_match_uses_crlf(tmp_path):
    content = b"def f():\r\n    a = 1\r\n    b = 2\r\n"
    res, after = _edit(tmp_path, content, "a = 1\nb = 2", "a = 10\nb = 20")
    assert not res.is_error, res.output
    assert after == b"def f():\r\n    a = 10\r\n    b = 20\r\n"


def test_tabs_in_file_spaces_in_anchor(tmp_path):
    content = b"def f():\n\tif x:\n\t\treturn 1\n"
    res, after = _edit(tmp_path, content, "    if x:\n        return 1",
                       "    if y:\n        return 2")
    assert not res.is_error, res.output
    assert after == b"def f():\n\tif y:\n\t\treturn 2\n"


def test_flattened_nesting_is_refused_not_misindented(tmp_path):
    res, after = _edit(tmp_path, SOURCE, "if x:\nreturn 1", "if y:\nreturn 2")
    assert res.is_error and after == SOURCE
    assert "indentation differs" in res.output and "lines 2-3" in res.output


def test_tolerant_delete_removes_whole_lines(tmp_path):
    res, after = _edit(tmp_path, SOURCE, "if x:\n    return 1\n", "")
    assert not res.is_error, res.output
    assert after == b"def f():\n    return 2\n"


def test_bench_row_counts_edit_file_failures():
    # bench/ is not in the public mirror: skip there instead of failing.
    bench_runner = pytest.importorskip("bench.runner")
    RunStats, TaskResult, _event_recorder = (bench_runner.RunStats, bench_runner.TaskResult,
                                             bench_runner._event_recorder)
    stats = RunStats()
    rec = _event_recorder(stats)
    for output, error in (("old_string not found in m.py.", True),
                          ("Edited m.py (1 replacement).", False),
                          ("Edited m.py (1 replacement). No exact match; used the unique "
                           "whitespace-tolerant match at line 2, re-indented", False)):
        rec("tool_start", {"name": "edit_file", "args": {"path": "m.py"}})
        rec("tool_end", {"name": "edit_file", "output": output, "error": error})
    rec("tool_start", {"name": "read_file", "args": {}})
    rec("tool_end", {"name": "read_file", "output": "nope", "error": True})
    rec("tool_error", {"name": "edit_file", "signature": "{}"})
    assert (stats.edit_failures, stats.edit_tolerant) == (2, 1)
    assert stats.tool_errors == 3
    assert {"edit_failures", "edit_tolerant"} <= set(TaskResult.__dataclass_fields__)
