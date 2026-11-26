"""2.61.0: the REPL's typed-input history file is redacted like sessions."""
import os
import stat

from prompt_toolkit.history import FileHistory

from wilbur.repl import redacting_file_history

FAKE = "not-a-real-wifi-pass-7731"


def _store(tmp_path, monkeypatch, line, cls_factory=redacting_file_history):
    secret_file = tmp_path / "Esp_Wifi.txt"
    secret_file.write_text(FAKE + "\n")
    monkeypatch.setenv("WILBUR_SECRET_FILES", str(secret_file))
    hist = tmp_path / "history"
    h = cls_factory(FileHistory, hist)
    h.append_string(line)
    return hist, h


def test_typed_secret_is_redacted_on_disk_and_file_is_0600(tmp_path, monkeypatch):
    hist, h = _store(tmp_path, monkeypatch, f"connect with {FAKE} please")
    text = hist.read_text()
    assert FAKE not in text
    assert "<REDACTED>" in text
    assert stat.S_IMODE(os.stat(hist).st_mode) == 0o600
    # the live (in-memory) recall still has what was typed
    assert FAKE in h.get_strings()[-1]


def test_pattern_secret_in_typed_line_is_redacted(tmp_path, monkeypatch):
    hist, _ = _store(tmp_path, monkeypatch, "export " + "API_" + "KEY=" + "abcd1234efgh")  # built at runtime: mirror scrub
    assert "abcd1234efgh" not in hist.read_text()


def test_plain_file_history_control_keeps_secret(tmp_path, monkeypatch):
    """Control: the stock FileHistory (the pre-2.61.0 behaviour) writes the
    secret, so the redaction test above can fail."""
    hist, _ = _store(tmp_path, monkeypatch, f"connect with {FAKE}",
                     cls_factory=lambda cls, p: cls(str(p)))
    assert FAKE in hist.read_text()


def test_ordinary_line_is_unchanged(tmp_path, monkeypatch):
    hist, _ = _store(tmp_path, monkeypatch, "fix the failing test in loop.py")
    assert "fix the failing test in loop.py" in hist.read_text()
