"""Unit tests pinning `wilbur.llm._balanced_json_objects`, the span scanner
that `recover_tool_calls` falls back to for bare JSON a model wrote into its
message body. Pure string scanning: no network, no GPU, no fixtures needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wilbur.llm import _balanced_json_objects


def test_single_object_in_prose():
    text = 'Sure, here is the call: {"name": "run_bash", "arguments": {"command": "ls"}} -- done.'
    spans = list(_balanced_json_objects(text))
    assert len(spans) == 1
    start, end = spans[0]
    assert text[start:end] == '{"name": "run_bash", "arguments": {"command": "ls"}}'


def test_two_objects():
    text = (
        '{"name": "read_file", "arguments": {"path": "a.txt"}} '
        'and then {"name": "write_file", "arguments": {"path": "b.txt", "content": "x"}}'
    )
    spans = list(_balanced_json_objects(text))
    assert len(spans) == 2
    assert text[spans[0][0]:spans[0][1]] == '{"name": "read_file", "arguments": {"path": "a.txt"}}'
    assert text[spans[1][0]:spans[1][1]] == '{"name": "write_file", "arguments": {"path": "b.txt", "content": "x"}}'
    # Spans are in order and non-overlapping.
    assert spans[0][1] <= spans[1][0]


def test_nested_braces():
    text = '{"name": "t", "arguments": {"outer": {"inner": {"deep": 1}}}}'
    spans = list(_balanced_json_objects(text))
    assert len(spans) == 1
    assert text[spans[0][0]:spans[0][1]] == text


def test_brace_inside_json_string():
    # Braces inside a string value must not change the depth count.
    text = '{"name": "t", "arguments": {"pattern": "a{b}c", "depth": 0}}'
    spans = list(_balanced_json_objects(text))
    assert len(spans) == 1
    assert text[spans[0][0]:spans[0][1]] == text
    # And an escaped quote inside the string must not end the string early,
    # so the brace after it is still inside the string.
    text2 = '{"name": "t", "arguments": {"s": "a\\"{b\\"c"}}'
    spans2 = list(_balanced_json_objects(text2))
    assert len(spans2) == 1
    assert text2[spans2[0][0]:spans2[0][1]] == text2


def test_unbalanced_input_yields_nothing():
    assert list(_balanced_json_objects('{"name": "t", "arguments": {"path": "a.txt"')) == []
    assert list(_balanced_json_objects('{"name": "t"')) == []
    # A stray closing brace before any opener must not start a span.
    assert list(_balanced_json_objects('} then {"name": "t"}')) == [(7, 20)]
    assert list(_balanced_json_objects('no json at all')) == []
