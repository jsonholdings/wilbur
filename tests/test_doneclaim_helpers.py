"""Unit tests for the pure helpers in wilbur.doneclaim.

Covers the cases tests/test_doneclaim.py does not: is_test_command on a
plain pytest invocation and on a non-test command, named_files pulling
paths out of a list of texts (including dedupe and version-number
filtering), and claims_done on a completion claim and on a reply that
does not claim completion. Pure functions only: no network, no GPU, no
config or session files touched (conftest's autouse isolation still
applies).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur import doneclaim


def test_is_test_command_pytest_and_non_test():
    assert doneclaim.is_test_command("python -m pytest tests/test_ops.py -q")
    assert doneclaim.is_test_command("npm test")
    assert not doneclaim.is_test_command("git status && ls -la")
    assert not doneclaim.is_test_command("npm install")


def test_named_files_pulls_paths_from_texts():
    texts = ["Create src/app/main.py and src/app/util.py",
             "then update README.md"]
    assert doneclaim.named_files(texts) == [
        "src/app/main.py", "src/app/util.py", "README.md"]


def test_named_files_dedupes_and_ignores_versions():
    assert doneclaim.named_files(["Create a.py", "Update a.py again"]) == ["a.py"]
    assert doneclaim.named_files(["bump to v2.3.4 and 1.2, e.g. like that"]) == []


def test_claims_done_positive():
    assert doneclaim.claims_done("The task is now complete.")
    assert doneclaim.claims_done("Everything is finished.")


def test_claims_done_negative():
    assert not doneclaim.claims_done("I'll start by reading the module first.")
    assert not doneclaim.claims_done("Still working on the second file.")
