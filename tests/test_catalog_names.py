"""`model_names` and `search` are pure data lookups: no network, no GPU."""
from __future__ import annotations

from wilbur import catalog


def test_model_names_all_is_nonempty_and_unique():
    names = catalog.model_names("all")
    assert names, "model_names('all') must not be empty"
    assert len(names) == len(set(names)), (
        f"duplicate names in model_names('all'): "
        f"{[n for n in set(names) if names.count(n) > 1]}")


def test_search_finds_every_name_from_model_names():
    """A name the installer would pull must be findable by `/search`."""
    for name in catalog.model_names("all"):
        hits = catalog.search(name)
        assert any(h.name == name for h in hits), (
            f"search({name!r}) did not return the entry named {name!r}")
