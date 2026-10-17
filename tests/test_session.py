"""Sessions must survive a crash, not just a clean exit."""
from __future__ import annotations

import json

import pytest

from wilbur import session as sessions
from wilbur.state import RunState


@pytest.fixture(autouse=True)
def _isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path / "s")


def _state():
    st = RunState(objective="rename foo to bar")
    st.set_plan([{"task": "one", "status": "completed"}, {"task": "two"}])
    st.record_failure("run_bash(command=x)", "exit 1")
    return st


def test_save_then_load_round_trips_state_not_just_messages():
    """Restoring the words and losing the thread is most of what matters on a
    local model, so the plan and ledger have to come back too."""
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert sessions.save("sid", "/tmp/proj", msgs, _state(), "m") is not None

    back = sessions.load("sid")
    assert back is not None
    assert back["messages"] == msgs
    assert back["state"].objective == "rename foo to bar"
    assert back["state"].progress == (1, 2)
    assert back["state"].repeated("run_bash(command=x)") == 1


def test_load_of_a_missing_session_is_none_not_an_exception():
    assert sessions.load("nope") is None


def test_forget_deletes_a_saved_session():
    """The documented way to clear a stale/queued session (owner-facing:
    `wilbur --forget ID`, or the VS Code sessions picker's trash button)."""
    msgs = [{"role": "user", "content": "hi"}]
    sessions.save("stale-sid", "/tmp/proj", msgs, _state(), "m")
    assert sessions.load("stale-sid") is not None

    assert sessions.forget("stale-sid") is True
    assert sessions.load("stale-sid") is None


def test_forget_of_a_missing_session_returns_false_not_an_exception():
    assert sessions.forget("never-existed") is False


def test_forgotten_session_is_not_picked_up_by_latest():
    """A forgotten session must never resurface via --continue."""
    msgs = [{"role": "user", "content": "hi"}]
    sessions.save("forget-me", "/tmp/proj", msgs, _state(), "m")
    sessions.forget("forget-me")
    assert sessions.latest("/tmp/proj") is None


def test_corrupt_session_file_is_none_not_an_exception():
    sessions.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    (sessions.SESSION_DIR / "bad.json").write_text("{not json")
    assert sessions.load("bad") is None
    assert sessions.listing() == []        # and must not poison the listing


def test_latest_is_scoped_to_the_directory():
    sessions.save("a", "/proj/one", [], RunState(objective="A"), "m")
    sessions.save("b", "/proj/two", [], RunState(objective="B"), "m")
    assert sessions.latest("/proj/one")["state"].objective == "A"
    assert sessions.latest("/proj/two")["state"].objective == "B"
    assert sessions.latest("/proj/three") is None


def test_transcript_is_bounded():
    """An unbounded transcript turns resume into a context overflow on turn
    one, which is a worse failure than losing the oldest messages."""
    msgs = [{"role": "user", "content": str(i)} for i in range(sessions.MAX_SAVED_MESSAGES * 3)]
    sessions.save("big", "/p", msgs, RunState(), "m")
    back = sessions.load("big")
    assert len(back["messages"]) == sessions.MAX_SAVED_MESSAGES
    # and it must keep the END, where the current work is
    assert back["messages"][-1]["content"] == str(len(msgs) - 1)


def test_save_never_raises_on_an_unwritable_directory(monkeypatch):
    """Losing a session file is an annoyance; taking down a working agent
    because a disk is full is not an acceptable trade."""
    def boom(*a, **k):
        raise OSError("read-only file system")
    monkeypatch.setattr(sessions.Path, "mkdir", boom)
    assert sessions.save("x", "/p", [], RunState(), "m") is None


def test_write_is_atomic(monkeypatch):
    """A half-written session is worse than none: resume would restore a
    truncated transcript and silently lose work."""
    seen = {}
    real_replace = sessions.Path.replace

    def spy(self, target):
        seen["replaced"] = True
        return real_replace(self, target)

    monkeypatch.setattr(sessions.Path, "replace", spy)
    sessions.save("atomic", "/p", [{"role": "user", "content": "x"}], RunState(), "m")
    assert seen.get("replaced"), "session was written in place rather than atomically"
    assert not list(sessions.SESSION_DIR.glob("*.tmp"))


def test_concurrent_writers_use_distinct_tmp_files(monkeypatch):
    """The incremental save and a oneshot's final save can write one session
    at once; a shared tmp name would interleave their bytes before the rename."""
    import threading
    tmp_names = []
    real_write = sessions.Path.write_text

    def spy(self, data, *a, **kw):
        tmp_names.append(self.name)
        return real_write(self, data, *a, **kw)

    monkeypatch.setattr(sessions.Path, "write_text", spy)
    worker = threading.Thread(target=sessions.save, args=("same", "/p", [], RunState(), "m"))
    worker.start()
    worker.join()
    sessions.save("same", "/p", [], RunState(), "m")
    assert len(tmp_names) == 2 and tmp_names[0] != tmp_names[1], tmp_names
    assert not list(sessions.SESSION_DIR.glob("*.tmp"))


def test_failed_rename_leaves_no_tmp_file(monkeypatch):
    def boom(self, target):
        raise OSError("disk full")
    monkeypatch.setattr(sessions.Path, "replace", boom)
    assert sessions.save("fails", "/p", [], RunState(), "m") is None
    assert not list(sessions.SESSION_DIR.glob("*.tmp"))
    assert not (sessions.SESSION_DIR / "fails.json").exists()


def test_listing_reports_newest_first():
    import time
    sessions.save("old", "/p", [], RunState(objective="old"), "m")
    time.sleep(0.01)
    sessions.save("new", "/p", [], RunState(objective="new"), "m")
    assert [r["id"] for r in sessions.listing()][:2] == ["new", "old"]
