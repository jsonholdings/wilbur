"""Config switches that turn a harness check off really turn it off."""
from types import SimpleNamespace

import wilbur.progress as progress
from wilbur.repl import Repl


def test_verify_done_claims_off_skips_gate():
    fake = SimpleNamespace(config=SimpleNamespace(verify_done_claims=False))
    assert Repl._done_claim_gate(fake, "All done.") is None


def test_progress_log_off_writes_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(progress, "append", lambda *a, **k: calls.append(a))
    Repl._log_progress(SimpleNamespace(config=SimpleNamespace(progress_log=False)), "x")
    assert calls == []


def test_progress_log_on_appends(monkeypatch):
    calls = []
    monkeypatch.setattr(progress, "append", lambda *a, **k: calls.append(a))
    Repl._log_progress(SimpleNamespace(config=SimpleNamespace(progress_log=True), session_id="s"), "ev", "d")
    assert calls == [("s", "ev", "d")]
