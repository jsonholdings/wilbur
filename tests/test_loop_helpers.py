"""Unit tests for the small helpers in wilbur/loop.py: slug() and recorded_ids().

No network, no GPU: pure string handling and a results file in tmp_path.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur import loop


def test_slug_lowercases_and_keeps_alnum_and_hyphens():
    assert loop.slug("Fix The Thing!") == "fix-the-thing"
    assert loop.slug("already-slug") == "already-slug"
    assert loop.slug("Task 42") == "task-42"


def test_slug_collapses_runs_of_other_characters_into_one_hyphen():
    assert loop.slug("a__b!!c") == "a-b-c"
    assert loop.slug("  spaced   out  ") == "spaced-out"
    assert loop.slug("CamelCase Words") == "camelcase-words"


def test_slug_trims_to_48_characters():
    long = "a" * 100
    assert loop.slug(long) == "a" * 48
    assert len(loop.slug("word " * 30)) <= 48


def test_slug_returns_task_for_empty_or_blank_input():
    assert loop.slug("") == "task"
    assert loop.slug("   ") == "task"
    assert loop.slug("---") == "task"


def test_recorded_ids_skips_malformed_lines(tmp_path):
    q = tmp_path / "queue.json"
    q.write_text("{}")
    results = tmp_path / "queue.json.results.jsonl"
    results.write_text(
        '{"id": "a", "status": "done"}\n'
        'not json at all\n'
        '{"status": "done"}\n'          # valid JSON, no id
        '{"id": "b"}\n'
    )
    assert loop.recorded_ids(str(q)) == {"a", "b"}


def test_recorded_ids_missing_results_file_is_empty(tmp_path):
    q = tmp_path / "queue.json"
    q.write_text("{}")
    assert loop.recorded_ids(str(q)) == set()
