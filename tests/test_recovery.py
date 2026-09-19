"""Tool-call recovery: the failure mode that made v1 unusable."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wilbur.llm import recover_tool_calls

KNOWN = {"run_bash", "read_file", "edit_file"}


def test_bare_json_is_recovered():
    """Exact output captured from huihui_ai/qwen2.5-coder-abliterate:32b."""
    content = '{"name": "run_bash", "arguments": {"command": "wc -l /etc/hostname"}}'
    calls, rest = recover_tool_calls(content, KNOWN)
    assert len(calls) == 1
    assert calls[0].name == "run_bash"
    assert calls[0].arguments == {"command": "wc -l /etc/hostname"}
    assert calls[0].recovered is True
    assert rest == ""


def test_tagged_call_is_recovered():
    content = ('Let me check.\n<tool_call>\n'
               '{"name":"read_file","arguments":{"path":"/tmp/a"}}\n</tool_call>')
    calls, rest = recover_tool_calls(content, KNOWN)
    assert [c.name for c in calls] == ["read_file"]
    assert "Let me check." in rest


def test_fenced_json_is_recovered():
    content = '```json\n{"name": "read_file", "arguments": {"path": "x.py"}}\n```'
    calls, _ = recover_tool_calls(content, KNOWN)
    assert calls[0].arguments == {"path": "x.py"}


def test_openai_nesting_is_accepted():
    content = '{"function": {"name": "run_bash", "arguments": {"command": "ls"}}}'
    calls, _ = recover_tool_calls(content, KNOWN)
    assert calls[0].name == "run_bash"


def test_string_encoded_arguments_are_parsed():
    content = '{"name": "run_bash", "arguments": "{\\"command\\": \\"ls\\"}"}'
    calls, _ = recover_tool_calls(content, KNOWN)
    assert calls[0].arguments == {"command": "ls"}


def test_unknown_tool_name_is_not_recovered():
    """Prose containing JSON must never be executed as a call."""
    content = 'The config looks like {"name": "widget", "arguments": {"x": 1}}'
    calls, rest = recover_tool_calls(content, KNOWN)
    assert calls == []
    assert rest == content


def test_code_block_the_user_asked_for_is_left_alone():
    content = 'Here is the schema:\n```json\n{"name": "server", "port": 8080}\n```'
    calls, _ = recover_tool_calls(content, KNOWN)
    assert calls == []


def test_json_containing_braces_in_strings():
    content = '{"name": "run_bash", "arguments": {"command": "echo \\"{}\\" > a.json"}}'
    calls, _ = recover_tool_calls(content, KNOWN)
    assert len(calls) == 1
    assert calls[0].arguments["command"] == 'echo "{}" > a.json'


def test_plain_prose_is_untouched():
    calls, rest = recover_tool_calls("I have finished the task.", KNOWN)
    assert calls == []
    assert rest == "I have finished the task."


# --------------------------------------------------------------------- #
# body-cleaning position accuracy (added iteration 14)


def test_duplicate_bare_calls_clean_both_spans():
    """Two identical bare calls must each be removed at their own offset, not
    collapsed onto the first occurrence (which corrupts the trailing text)."""
    one = '{"name": "run_bash", "arguments": {"command": "ls"}}'
    content = f"pre {one} mid {one} post"
    calls, cleaned = recover_tool_calls(content, {"run_bash"})
    assert len(calls) == 2
    assert cleaned == "pre  mid  post"


def test_cleaned_text_survives_a_call_between_prose():
    content = 'Here you go. {"name":"run_bash","arguments":{"command":"pwd"}} Done.'
    calls, cleaned = recover_tool_calls(content, {"run_bash"})
    assert len(calls) == 1
    assert cleaned == "Here you go.  Done."


def test_two_different_bare_calls_both_recovered_and_removed():
    a = '{"name":"read_file","arguments":{"path":"a"}}'
    b = '{"name":"run_bash","arguments":{"command":"ls"}}'
    calls, cleaned = recover_tool_calls(f"x {a} y {b} z", {"read_file", "run_bash"})
    assert [c.name for c in calls] == ["read_file", "run_bash"]
    assert cleaned == "x  y  z"


def test_json_that_is_not_a_call_is_left_in_the_body():
    content = 'Config: {"host": "localhost", "port": 5432} works.'
    calls, cleaned = recover_tool_calls(content, {"run_bash"})
    assert calls == []
    assert cleaned == content


def test_nested_braces_in_a_call_do_not_truncate_the_span():
    content = '{"name":"run_bash","arguments":{"command":"echo {ok}"}}'
    calls, cleaned = recover_tool_calls(content, {"run_bash"})
    assert len(calls) == 1
    assert calls[0].arguments["command"] == "echo {ok}"
    assert cleaned == ""
