"""`_walk_for_models` must find model entries anywhere inside an arbitrary
decoded JSON blob, and must ignore everything that is not a model entry.
Hand-built nested dicts and lists only: no network, no GPU."""
from __future__ import annotations

from wilbur import registry


def test_models_are_found_at_depth():
    """A model entry must be found no matter how deeply it is nested in
    dicts and lists, whether it uses `name` or `model` and `description`
    or `summary`."""
    blob = {
        "page": {
            "results": [
                {"name": "qwen2.5-coder", "description": "a coder model"},
                {"other": "noise", "nested": {"model": "llama3.1", "summary": "a general model"}},
                {"deep": {"deeper": [{"model": "mistral:7b", "description": "tagged"}]}},
            ]
        }
    }
    found = {item["name"]: item["desc"] for item in registry._walk_for_models(blob)}
    assert found == {
        "qwen2.5-coder": "a coder model",
        "llama3.1": "a general model",
        "mistral:7b": "tagged",
    }


def test_non_model_nodes_are_ignored():
    """Nodes without a usable name, with a malformed name, or with a
    non-string description must not be reported; scalars and None must
    not raise."""
    blob = {
        "no_name_key": {"description": "not a model"},
        "empty_name": {"name": "", "description": "empty"},
        "bad_name": {"name": "Qwen 2.5", "description": "spaces and caps"},
        "bad_name2": {"name": ".leading-dot", "description": "starts with dot"},
        "non_string_name": {"name": 42, "description": "number"},
        "non_string_desc": {"name": "good-model", "description": {"nested": "dict"}},
        "scalar": "just a string",
        "number": 3.14,
        "null": None,
        "bool": True,
    }
    assert registry._walk_for_models(blob) == []
    assert registry._walk_for_models(None) == []
    assert registry._walk_for_models("a bare string") == []
    assert registry._walk_for_models(42) == []
    assert registry._walk_for_models([]) == []


def test_description_is_normalized_and_truncated():
    """Whitespace runs collapse to single spaces and the description is
    capped at 300 characters, so a scraped blob cannot leak a wall of
    markup into the result list."""
    long_desc = "word " * 200  # 1000 chars with trailing space
    blob = {"name": "some-model", "description": "  leading   spaces\nand\ttabs  " + long_desc}
    found = registry._walk_for_models(blob)
    assert len(found) == 1
    desc = found[0]["desc"]
    assert desc == " ".join(desc.split()), "whitespace runs must collapse"
    assert len(desc) == 300, "description must be capped at 300 chars"


def test_model_entry_with_missing_description_is_still_found():
    """A valid name with no description at all is still a model entry,
    with an empty description."""
    blob = {"name": "bare-model"}
    found = registry._walk_for_models(blob)
    assert found == [{"name": "bare-model", "desc": ""}]
