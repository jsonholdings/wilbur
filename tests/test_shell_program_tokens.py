"""Unit tests for wilbur.tools.shell._program_tokens and elevation_program.

Covers:
  * _program_tokens directly: env-assignment prefixes, wrapper chains,
    quoted/escaped arguments, the shlex fallback on unbalanced quotes,
    and edge cases (empty segment, bare assignment, non-identifier prefix).
  * elevation_program: pipelines, && / ; / || chains, nested wrappers,
    duration-suffixed timeout args, and plain commands that must return None.

Cases already in tests/test_shell_elevation_guard.py are not duplicated here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.tools.shell import _program_tokens, elevation_program


# ---------------------------------------------------------------------------
# _program_tokens: direct unit tests
# ---------------------------------------------------------------------------

def test_program_tokens_bare_sudo():
    assert _program_tokens("sudo ls") == ["sudo"]


def test_program_tokens_single_assignment_prefix():
    assert _program_tokens("FOO=1 sudo ls") == ["sudo"]


def test_program_tokens_multiple_assignment_prefixes():
    assert _program_tokens("FOO=1 BAR=2 sudo ls") == ["sudo"]


def test_program_tokens_assignment_with_equals_in_value():
    # FOO=bar=baz: split("=", 1)[0] is "FOO", an identifier → skipped
    assert _program_tokens("FOO=bar=baz sudo ls") == ["sudo"]


def test_program_tokens_double_equals_in_name():
    # FOO=1=2: split("=", 1)[0] is "FOO", an identifier → skipped
    assert _program_tokens("FOO=1=2 sudo ls") == ["sudo"]


def test_program_tokens_non_identifier_prefix_is_program():
    # FOO-BAR=1: "FOO-BAR" is not an identifier → treated as the program
    assert _program_tokens("FOO-BAR=1 sudo ls") == ["FOO-BAR=1"]


def test_program_tokens_bare_assignment_only():
    assert _program_tokens("FOO=1") == []


def test_program_tokens_empty_segment():
    assert _program_tokens("") == []


def test_program_tokens_single_word():
    assert _program_tokens("sudo") == ["sudo"]


def test_program_tokens_env_wrapper():
    assert _program_tokens("env sudo ls") == ["env", "sudo"]


def test_program_tokens_env_with_flag_and_assignment():
    # env -i FOO=1 sudo ls: "-i" is an option, "FOO=1" is an assignment,
    # "sudo" is the first non-wrapper program → collection stops there
    assert _program_tokens("env -i FOO=1 sudo ls") == ["env", "sudo"]


def test_program_tokens_timeout_wrapper_with_seconds():
    assert _program_tokens("timeout 5 doas ls") == ["timeout", "doas"]


def test_program_tokens_timeout_wrapper_with_duration_suffix():
    # "5s" matches ^[0-9.]+[smhd]?$ → skipped as a duration argument
    assert _program_tokens("timeout 5s doas ls") == ["timeout", "doas"]


def test_program_tokens_nice_wrapper_with_numeric_arg():
    assert _program_tokens("nice -n 10 sudo ls") == ["nice", "sudo"]


def test_program_tokens_xargs_wrapper_with_numeric_arg():
    assert _program_tokens("xargs -P 4 sudo rm") == ["xargs", "sudo"]


def test_program_tokens_nested_env_wrappers():
    assert _program_tokens("env env sudo ls") == ["env", "env", "sudo"]


def test_program_tokens_nested_timeout_wrappers():
    assert _program_tokens("timeout 5 timeout 5 sudo ls") == [
        "timeout", "timeout", "sudo"]


def test_program_tokens_nested_nice_wrappers():
    assert _program_tokens("nice -n 10 nice -n 5 sudo ls") == [
        "nice", "nice", "sudo"]


def test_program_tokens_single_quoted_program():
    # shlex strips the quotes; "sudo" is not a wrapper → collection stops
    assert _program_tokens("'sudo' ls") == ["sudo"]


def test_program_tokens_double_quoted_program():
    assert _program_tokens('"sudo" ls') == ["sudo"]


def test_program_tokens_escaped_program():
    # shlex strips the backslash
    assert _program_tokens("\\sudo ls") == ["sudo"]


def test_program_tokens_shlex_fallback_unbalanced_quote():
    # shlex.split raises ValueError on unbalanced quotes → segment.split()
    # "echo" is not a wrapper → break after first word
    assert _program_tokens("echo 'unbalanced sudo ls") == ["echo"]


def test_program_tokens_shlex_fallback_wrapper_with_unbalanced_quote():
    # "env" is a wrapper → continue; "'unbalanced" is not an option or
    # numeric → becomes the "program" and breaks the loop
    assert _program_tokens("env 'unbalanced sudo ls") == ["env", "'unbalanced"]


def test_program_tokens_comment_stripped():
    # shlex.split(comments=True) strips "# sudo"
    assert _program_tokens("echo hi # sudo") == ["echo"]


def test_program_tokens_env_with_comment():
    assert _program_tokens("env # sudo") == ["env"]


# ---------------------------------------------------------------------------
# elevation_program: edge cases not in test_shell_elevation_guard.py
# ---------------------------------------------------------------------------

def test_elevation_pipeline_three_segments():
    assert elevation_program("ls | grep x | sudo tee /etc/f") == "sudo"


def test_elevation_and_chain_three_segments():
    assert elevation_program("a && b && sudo c") == "sudo"


def test_elevation_semicolon_chain_three_segments():
    assert elevation_program("a; b; sudo c") == "sudo"


def test_elevation_or_chain_three_segments():
    assert elevation_program("a || b || sudo c") == "sudo"


def test_elevation_nested_env_wrappers():
    assert elevation_program("env env sudo ls") == "sudo"


def test_elevation_nested_timeout_wrappers():
    assert elevation_program("timeout 5 timeout 5 sudo ls") == "sudo"


def test_elevation_nested_nice_wrappers():
    assert elevation_program("nice -n 10 nice -n 5 sudo ls") == "sudo"


def test_elevation_env_with_flag_and_assignment():
    assert elevation_program("env -i FOO=1 sudo ls") == "sudo"


def test_elevation_timeout_with_duration_suffix():
    assert elevation_program("timeout 5s doas ls") == "doas"


def test_elevation_nice_with_numeric_arg():
    assert elevation_program("nice -n 10 sudo ls") == "sudo"


def test_elevation_xargs_with_numeric_arg():
    assert elevation_program("xargs -P 4 sudo rm") == "sudo"


def test_elevation_multiple_assignment_prefixes():
    assert elevation_program("FOO=1 BAR=2 sudo ls") == "sudo"


def test_elevation_assignment_with_equals_in_value():
    assert elevation_program("FOO=bar=baz sudo ls") == "sudo"


def test_elevation_double_quoted_program():
    assert elevation_program('"sudo" ls') == "sudo"


def test_elevation_escaped_program():
    assert elevation_program("\\sudo ls") == "sudo"


def test_elevation_sudoedit():
    assert elevation_program("sudoedit /etc/f") == "sudoedit"


def test_elevation_run0():
    assert elevation_program("run0 ls") == "run0"


def test_elevation_su_with_dash_c():
    assert elevation_program("su -c id") == "su"


def test_elevation_shlex_fallback_unbalanced_quote():
    # "echo" is not a wrapper → only ["echo"] → None
    assert elevation_program("echo 'unbalanced sudo ls") is None


def test_elevation_shlex_fallback_wrapper_unbalanced_quote():
    # ["env", "'unbalanced"] → neither is an elevation program → None
    assert elevation_program("env 'unbalanced sudo ls") is None


def test_elevation_comment_stripped():
    assert elevation_program("echo hi # sudo") is None


def test_elevation_env_with_comment():
    assert elevation_program("env # sudo") is None


def test_elevation_non_identifier_prefix_is_program():
    # FOO-BAR=1 is treated as the program → not an elevation program
    assert elevation_program("FOO-BAR=1 sudo ls") is None


def test_elevation_bare_assignment_only():
    assert elevation_program("FOO=1") is None


def test_elevation_empty_command():
    assert elevation_program("") is None


def test_elevation_plain_echo():
    assert elevation_program("echo hello") is None


def test_elevation_quoted_sudo_as_argument():
    # "sudo" is an argument to echo, not the program
    assert elevation_program('echo "sudo"') is None


def test_elevation_quoted_sudo_single_quotes():
    assert elevation_program("echo 'sudo'") is None


def test_elevation_sudo_in_path_argument():
    # /usr/bin/sudo is an argument to ls, not the program
    assert elevation_program("ls /usr/bin/sudo") is None


def test_elevation_env_wrapper_plain_command():
    assert elevation_program("env ls") is None


def test_elevation_env_with_assignment_plain_command():
    assert elevation_program("env FOO=1 ls") is None


def test_elevation_timeout_plain_command():
    assert elevation_program("timeout 5 ls") is None


def test_elevation_nice_plain_command():
    assert elevation_program("nice -n 10 ls") is None


def test_elevation_xargs_plain_command():
    assert elevation_program("xargs -P 4 ls") is None


def test_elevation_assignment_prefix_plain_command():
    assert elevation_program("FOO=1 ls") is None


def test_elevation_multiple_assignment_prefix_plain_command():
    assert elevation_program("FOO=1 BAR=2 ls") is None
