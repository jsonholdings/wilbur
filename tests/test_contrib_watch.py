"""contrib/wilbur-watch/watch.sh reads `wilbur status --check`, not file mtimes.

A fake wilbur executable stands in for the CLI, so no Wilbur run, model or
network is touched. Ollama is pointed at a closed local port.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

CONTRIB = Path(__file__).resolve().parents[1] / "contrib"
WATCH = CONTRIB / "wilbur-watch" / "watch.sh"

if not CONTRIB.is_dir():
    # contrib/ is private-repo only; the public mirror does not ship it.
    pytest.skip("contrib/ not shipped in this checkout", allow_module_level=True)


def _fake_wilbur(tmp_path: Path, exit_code: int, stderr_line: str = "") -> Path:
    fake = tmp_path / "fake-wilbur"
    fake.write_text(
        "#!/bin/sh\n"
        '[ "$1" = status ] || exit 9\n'
        'echo "STATE  REASON  SINCE  LAST  PID  CWD"\n'
        'echo "working -  10:00:00 10:01:00 4242  /srv/proj"\n'
        + (f'echo "{stderr_line}" >&2\n' if stderr_line else "")
        + f"exit {exit_code}\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def _run(tmp_path: Path, wilbur: str) -> tuple[int, str, list[str]]:
    notified = tmp_path / "notified.txt"
    notifier = tmp_path / "notifier"
    notifier.write_text(f'#!/bin/sh\necho "$1|$2" >> "{notified}"\n')
    notifier.chmod(notifier.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env.update(
        WILBUR_BIN=wilbur,
        WILBUR_WATCH_DIR=str(tmp_path / "watch"),
        WILBUR_WATCH_NOTIFY=str(notifier),
        OLLAMA_URL="http://127.0.0.1:9",
    )
    proc = subprocess.run(["/bin/sh", str(WATCH)], env=env, timeout=30,
                          capture_output=True, text=True)
    log = (tmp_path / "watch" / "wilbur-watch.log").read_text()
    calls = notified.read_text().splitlines() if notified.exists() else []
    return proc.returncode, log, calls


def test_ok_run_logs_rows_and_does_not_notify(tmp_path):
    rc, log, calls = _run(tmp_path, str(_fake_wilbur(tmp_path, 0)))
    assert rc == 0
    assert "RUN       working" in log
    assert "OK        wilbur status --check exit 0" in log
    assert "OLLAMA    UNREACHABLE" in log
    assert calls == []


def test_alert_notifies_once_per_transition(tmp_path):
    fake = str(_fake_wilbur(tmp_path, 1, "s1: user message unread for 3.0 min"))
    rc, log, calls = _run(tmp_path, fake)
    assert rc == 1
    assert "ALERT" in log and "CHECK     s1: user message unread" in log
    assert len(calls) == 1 and calls[0].startswith("Wilbur needs attention|s1:")
    # A second tick still in ALERT must not notify again.
    rc, _, calls = _run(tmp_path, fake)
    assert rc == 1 and len(calls) == 1
    # Recovery, then a new ALERT, notifies again.
    _run(tmp_path, str(_fake_wilbur(tmp_path, 0)))
    rc, _, calls = _run(tmp_path, str(_fake_wilbur(tmp_path, 1)))
    assert rc == 1 and len(calls) == 2


def test_unknown_when_status_cannot_read(tmp_path):
    rc, log, calls = _run(tmp_path, str(_fake_wilbur(tmp_path, 2)))
    assert rc == 2
    assert "UNKNOWN   wilbur status --check exit 2" in log
    assert calls == []


def test_missing_wilbur_is_unknown_not_ok(tmp_path):
    rc, log, _ = _run(tmp_path, str(tmp_path / "no-such-wilbur"))
    assert rc == 2
    assert "UNKNOWN   wilbur executable not found" in log
    assert "OK " not in log
