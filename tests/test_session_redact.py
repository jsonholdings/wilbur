"""secrets-in-session-log: saved sessions are 0600 and redacted; live data is not."""
import copy
import json
import stat

import pytest

from wilbur import config, session as sessions
from wilbur.state import RunState

FAKE = "Fake-Wifi-Pass-9931"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path / "s")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    secret_file = tmp_path / "Esp_Wifi.txt"
    secret_file.write_text(FAKE + "\n")
    monkeypatch.setenv("WILBUR_SECRET_FILES", str(secret_file))


def _save(messages):
    path = sessions.save("t1", "/w", messages, RunState(objective="x"), "m")
    assert path is not None
    return path, json.loads(path.read_text())


def _call(cmd):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": "bash", "arguments": {"command": cmd}}}]}


def test_file_mode_0600_and_dir_0700():
    path, _ = _save([{"role": "user", "content": "hi"}])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_configured_secret_redacted_in_args_and_output():
    msgs = [_call(f"nmcli dev wifi connect Home password {FAKE}"),
            {"role": "tool", "content": f"cat ~/Esp_Wifi.txt\n{FAKE}\n"}]
    _, data = _save(msgs)
    raw = json.dumps(data)
    assert FAKE not in raw
    assert "<REDACTED>" in data["messages"][0]["tool_calls"][0]["function"]["arguments"]["command"]
    assert "<REDACTED>" in data["messages"][1]["content"]


def test_configured_secret_via_config_key(tmp_path, monkeypatch):
    monkeypatch.delenv("WILBUR_SECRET_FILES")
    other = tmp_path / "other.txt"
    other.write_text("Zq7-only-in-config-file\n")
    (tmp_path / "config.json").write_text(json.dumps({"secret_files": [str(other)]}))
    _, data = _save([{"role": "tool", "content": "got Zq7-only-in-config-file ok"}])
    assert "Zq7-only-in-config-file" not in json.dumps(data)


@pytest.mark.parametrize("text,secret", [
    ("export API_KEY" + "=abcd1234efgh", "abcd1234efgh"),  # split: mirror scrub
    ("curl -H 'Authorization: Bearer tok_ZZ998877'", "tok_ZZ998877"),
    ("token ghp_aaaaBBBBccccDDDDeeee1111", "ghp_aaaaBBBBccccDDDDeeee1111"),
    ("key sk-proj-abcdefghijklmnop1234", "sk-proj-abcdefghijklmnop1234"),
    ("password" + "=hunter2xyz", "hunter2xyz"),
])
def test_pattern_secret_redacted(text, secret):
    _, data = _save([{"role": "user", "content": text}])
    assert secret not in json.dumps(data)
    assert "<REDACTED>" in data["messages"][0]["content"]


def test_live_messages_unchanged():
    msgs = [_call(f"connect password {FAKE}")]
    before = copy.deepcopy(msgs)
    _save(msgs)
    assert msgs == before
    assert FAKE in msgs[0]["tool_calls"][0]["function"]["arguments"]["command"]


def test_control_normal_message_saved_unchanged():
    msg = {"role": "user", "content": "rename foo to bar in src/app.py, then run pytest -q"}
    _, data = _save([msg])
    assert data["messages"][0] == msg


def test_prose_password_word_is_not_redacted_control():
    from wilbur.redact import redact
    msgs = [{"role": "assistant", "content": "Use the password reset page."}]
    assert redact(msgs) == msgs
