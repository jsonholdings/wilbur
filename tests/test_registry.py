"""`/search` must work offline and must never lie about where a result came from."""
from __future__ import annotations

from wilbur import catalog, registry


def test_local_search_needs_no_network(monkeypatch):
    """The catalog half must never depend on ollama.com. If a network failure
    can empty the results, the feature is useless on the air-gapped machines
    this whole project is built for."""
    def explode(*a, **k):
        raise OSError("network is down")
    monkeypatch.setattr(registry.urllib.request, "urlopen", explode)
    results = registry.search("coder")
    assert results, "a network failure emptied the search"
    assert all(r.source == "local" for r in results)


def test_remote_failure_is_swallowed_not_raised(monkeypatch):
    monkeypatch.setattr(registry.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    assert registry._remote("anything") == []


def test_results_are_labelled_by_source(monkeypatch):
    monkeypatch.setattr(registry, "_remote", lambda q: [
        registry.Result(name="some-remote-model", description="d", source="ollama.com")])
    results = registry.search("qwen")
    assert {r.source for r in results} == {"local", "ollama.com"}
    for r in results:
        # A remote result has no size, and must not borrow a local one.
        if r.source != "local":
            assert r.gib is None and not r.measured


def test_unpullable_model_is_findable_and_flagged():
    """Searching for it must explain why it cannot be installed rather than
    returning nothing and letting the user try a pull that fails."""
    hits = [r for r in registry.search("coder-expert", remote=False)]
    assert hits and not hits[0].pullable
    assert "ollama create" in hits[0].description or "build" in hits[0].description.lower()


def test_installed_models_are_marked():
    installed = {catalog.DEFAULT_MODEL}
    results = registry.search(catalog.DEFAULT_MODEL, remote=False, installed=installed)
    assert any(r.installed for r in results)


def test_empty_query_lists_the_whole_catalog():
    results = registry.search("", remote=False)
    assert len(results) >= len(catalog.MODELS)


def test_remote_duplicates_of_local_entries_are_dropped(monkeypatch):
    """ollama.com will return `qwen2.5-coder` for a query the catalog already
    answers with `qwen2.5-coder:32b`; showing both is noise."""
    monkeypatch.setattr(registry, "_remote", lambda q: [
        registry.Result(name="qwen2.5-coder", description="dup", source="ollama.com")])
    names = [r.name for r in registry.search("qwen2.5-coder")]
    assert "qwen2.5-coder" not in names
