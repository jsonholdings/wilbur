"""`catalog.search` and `catalog.model_names` are pure data lookups:
no network, no GPU, no filesystem (the conftest still isolates the real
~/.config/wilbur and ~/.local for every test)."""
from __future__ import annotations

import pytest

from wilbur import catalog


def test_search_finds_entry_by_name_substring():
    """A substring of an entry's name must surface that entry."""
    hits = catalog.search("nomic-embed")
    assert any(h.name == "nomic-embed-text" for h in hits), (
        f"search('nomic-embed') did not return nomic-embed-text: "
        f"{[h.name for h in hits]}")


def test_search_is_case_insensitive():
    """The docstring promises case-insensitive matching; verify it."""
    hits = catalog.search("QWEN3")
    assert any(h.name == "qwen3:14b" for h in hits), (
        f"search('QWEN3') did not return qwen3:14b: {[h.name for h in hits]}")


def test_search_returns_empty_list_for_nonsense():
    hits = catalog.search("zzz-not-a-model-zzz")
    assert hits == []


def test_model_names_each_include_value_returns_list_of_strings():
    for include in ("required", "default", "core", "all"):
        names = catalog.model_names(include)
        assert isinstance(names, list), (
            f"model_names({include!r}) must return a list, got {type(names)}")
        assert names, f"model_names({include!r}) must not be empty"
        assert all(isinstance(n, str) for n in names), (
            f"model_names({include!r}) must contain only strings: {names!r}")


def test_model_names_default_is_the_documented_default():
    """The signature default is 'all'; calling with no argument must agree."""
    assert catalog.model_names() == catalog.model_names("all")
