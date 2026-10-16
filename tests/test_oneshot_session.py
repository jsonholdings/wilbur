"""`wilbur -p` must leave a resumable session, always as a NEW one."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.__main__ as main_mod
import wilbur.agent as agent_mod
from wilbur import session as sessions
from wilbur.config import Config
from wilbur.fake_backend import FakeClient
from wilbur.state import RunState

from conftest import say


def test_oneshot_saves_a_new_session_that_continue_finds(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(agent_mod, "OllamaClient", FakeClient([say("all done")]))
    old = sessions.save("old-1", str(tmp_path), [], RunState(objective="stale"), "m")
    assert old is not None
    assert main_mod._oneshot(Config(), str(tmp_path), "do the thing", auto=True) == 0
    saved = sessions.latest(str(tmp_path))
    assert saved is not None and saved["id"] != "old-1"
    assert saved["objective"] == "do the thing"
    assert any(m.get("content") == "all done" for m in saved["messages"])


def test_oneshot_failure_still_saves(monkeypatch, tmp_path, capsys):
    class Boom(FakeClient):
        def chat(self, *a, **kw):
            raise RuntimeError("backend died")
    monkeypatch.setattr(agent_mod, "OllamaClient", Boom([]))
    assert main_mod._oneshot(Config(), str(tmp_path), "try it", auto=True) == 1
    saved = sessions.latest(str(tmp_path))
    assert saved is not None and saved["objective"] == "try it"
