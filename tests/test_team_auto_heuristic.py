"""Labelled real-world prompts for the `/team auto` sizing heuristic.

Prompts are phrased from this repo's BACKLOG items and commit subjects and
hand-labelled: True = team (multi-file / risky / cross-cutting), False =
single agent (one small, contained change).
"""
import pytest

from wilbur.team import needs_team

LABELLED = [
    # team
    ("Wire the gpu-lease client into loop mode and the voice service", True),
    ("Update wilbur/team.py and tests/test_team.py so the tester must go red", True),
    ("Add gq request signatures to the client and wilbur end to end", True),
    ("Refactor the agent loop to support retry/backoff for every tool call", True),
    ("Migrate the session store from JSON files to SQLite", True),
    ("Add a run_tests tool, register it in the agent and the tests", True),
    ("Make loop --once exit 2 when no task ran; update the CLI and the tests", True),
    ("Log tool calls across the coder, tester and reviewer roles", True),
    # single
    ("fix typo in README.md", False),
    ("Bump the version from 2.22.2 to 2.22.3 in wilbur/__init__.py", False),
    ("Refactor this function in wilbur/worktree.py to return early", False),
    ("In wilbur/cli.py change the idle sign-off, e.g. to 'MAY I HAVE ANOTHER?'", False),
    ("Show the running version in the startup box", False),
    ("Make the GQ 401 error louder in gpu_priority.py", False),
    ("Add a --json flag to the sessions command", False),
    ("Fix the README image URL, i.e. use an absolute link", False),
    ("Rename tmp to lease_path in wilbur/loop.py (since v2.21.0)", False),
    ("Explain why the voice service fails on Python 3.12", False),
]


@pytest.mark.parametrize("prompt,expected", LABELLED)
def test_team_auto_labelled(prompt, expected):
    assert needs_team(prompt, "auto") is expected


@pytest.mark.parametrize("prompt,_", LABELLED)
def test_team_on_off_override(prompt, _):
    assert needs_team(prompt, "on") is True
    assert needs_team(prompt, "off") is False
