"""flaky-test-worktree-teardown (2.61.0): the real-home guard counts only this
process's writes, so the owner's live Wilbur writing its history mid-suite no
longer fails the last test's teardown."""
import subprocess
import sys

import conftest


def _guard(monkeypatch, path):
    monkeypatch.setattr(conftest, "_GUARDED_REAL_PATHS", {str(path)})
    monkeypatch.setattr(conftest, "_GUARDED_NAMES", {path.name})
    start = len(conftest._REAL_PATH_WRITES)
    return start


def _drain(start):
    got = conftest._REAL_PATH_WRITES[start:]
    del conftest._REAL_PATH_WRITES[start:]
    return got


def test_another_process_writing_the_real_history_is_not_a_leak(tmp_path, monkeypatch):
    hist = tmp_path / "history"
    hist.write_text("old\n")
    start = _guard(monkeypatch, hist)
    subprocess.run([sys.executable, "-c",
                    f"open({str(hist)!r}, 'a').write('live wilbur line\\n')"], check=True)
    assert _drain(start) == []
    # the old mtime check would have flagged exactly this
    assert hist.read_text().endswith("live wilbur line\n")


def test_this_process_writing_a_guarded_path_is_recorded_control(tmp_path, monkeypatch):
    hist = tmp_path / "history"
    start = _guard(monkeypatch, hist)
    hist.write_text("leaked by a test\n")
    with open(hist, "a") as f:
        f.write("again\n")
    got = _drain(start)
    assert len(got) == 2 and all(str(hist) in g for g in got)


def test_reading_a_guarded_path_is_not_recorded(tmp_path, monkeypatch):
    hist = tmp_path / "history"
    hist.write_text("x\n")
    start = _guard(monkeypatch, hist)
    hist.read_text()
    assert _drain(start) == []
