"""The per-test shared-state guard in tests/conftest.py (`_no_leaked_state`).

PR #1 (2.25.0) review: tests/test_doneclaim.py failed in the full suite but
passed alone, the signature of a test leaking process-wide state into the
next one. The guard names any such leak on the test that caused it.
"""
from __future__ import annotations

import io
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.agent as agent_mod
import wilbur.config as config_mod
import wilbur.verify as verify_mod
from wilbur import fake_backend

from conftest import _leaked, _restore, _shared_state

REPO = Path(__file__).resolve().parents[1]
ALL_KEYS = list(_shared_state())


def test_unchanged_state_is_not_a_leak():
    assert _leaked(_shared_state(), _shared_state()) == []


def _leak_everything(tmp_path):
    """Change all 8 guarded pieces of state directly, with no monkeypatch to
    undo them -- exactly what a leaking test does."""
    os.environ["WILBUR_LEAK_PROBE"] = "1"
    os.chdir(tmp_path)
    sys.stdin = io.StringIO("")
    signal.signal(signal.SIGINT, lambda *_: None)
    agent_mod.OllamaClient = object()
    config_mod.CONFIG_PATH = tmp_path / "leaked.json"
    verify_mod.detect = lambda *a, **kw: None
    script = tmp_path / "s.json"
    script.write_text("[]")
    fake_backend.shared_client(script)


def test_each_kind_of_leak_is_named_and_restored(tmp_path):
    before = _shared_state()
    try:
        _leak_everything(tmp_path)
        leaked = _leaked(before, _shared_state())
    finally:
        _restore(before, ALL_KEYS)
    assert leaked == ALL_KEYS
    assert len(ALL_KEYS) == 8
    assert _leaked(before, _shared_state()) == []
    assert "WILBUR_LEAK_PROBE" not in os.environ


def _run_suite(pytester, monkeypatch, body: str):
    """Run `body` as a test file under a copy of this suite's conftest, in a
    subprocess (so its leaks cannot reach this process)."""
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    pytester.makeconftest((REPO / "tests" / "conftest.py").read_text())
    pytester.makepyfile(test_probe=body)
    return pytester.runpytest_subprocess(
        "-p", "no:randomly", "-p", "no:cacheprovider", "-W", "default")


def test_guard_fails_a_leaking_test_and_the_next_test_still_passes(pytester, monkeypatch):
    result = _run_suite(pytester, monkeypatch, """
import os, sys, signal, io
import wilbur.agent as agent_mod
import wilbur.config as config_mod
import wilbur.verify as verify_mod
from wilbur.llm import OllamaClient

START = (os.getcwd(), sys.stdin, signal.getsignal(signal.SIGINT),
         config_mod.CONFIG_PATH, verify_mod.detect)

def test_a_leaks_everything(tmp_path):
    os.environ["WILBUR_LEAK_PROBE"] = "1"
    os.chdir(tmp_path)
    sys.stdin = io.StringIO("")
    signal.signal(signal.SIGINT, lambda *_: None)
    agent_mod.OllamaClient = object()
    config_mod.CONFIG_PATH = tmp_path / "leaked.json"
    verify_mod.detect = lambda *a, **kw: None

def test_b_sees_clean_state():
    assert "WILBUR_LEAK_PROBE" not in os.environ
    assert agent_mod.OllamaClient is OllamaClient
    assert (os.getcwd(), sys.stdin, signal.getsignal(signal.SIGINT),
            verify_mod.detect) == (START[0], START[1], START[2], START[4])
    assert config_mod.CONFIG_PATH.name != "leaked.json"
""")
    result.assert_outcomes(passed=2, errors=1)
    # CONFIG_PATH is not named: isolate_wilbur_home monkeypatches it for every
    # test, and undoing that patch already puts the original back before the
    # guard looks, so a direct rebind of it cannot reach the next test.
    result.stdout.fnmatch_lines([
        "*ERROR at teardown of test_a_leaks_everything*",
        "*test leaked shared state: os.environ, cwd, sys.stdin, SIGINT handler, "
        "wilbur.agent.OllamaClient, wilbur.verify.detect",
    ])


def test_guard_runs_after_monkeypatch_is_undone(pytester, monkeypatch):
    """The guard's teardown sees the state after every monkeypatch is undone,
    so changes made through monkeypatch are never reported as leaks."""
    result = _run_suite(pytester, monkeypatch, """
import os
import wilbur.agent as agent_mod

def test_patches_through_monkeypatch(monkeypatch, tmp_path):
    monkeypatch.setenv("WILBUR_LEAK_PROBE", "1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "OllamaClient", object())
""")
    result.assert_outcomes(passed=1)


def test_guard_warns_about_a_thread_left_running(pytester, monkeypatch):
    result = _run_suite(pytester, monkeypatch, """
import threading

_stop = threading.Event()

def test_starts_a_thread():
    threading.Thread(target=_stop.wait, name="probe-pump", daemon=True).start()

def test_stops_it():
    _stop.set()
""")
    result.assert_outcomes(passed=2, warnings=1)
    result.stdout.fnmatch_lines(["*LeakedThreadWarning: test left threads running: probe-pump (daemon)*"])


def test_pytest_current_test_is_ignored(monkeypatch):
    before = _shared_state()
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "other")
    assert _leaked(before, _shared_state()) == []
    assert os.environ["PYTEST_CURRENT_TEST"] == "other"
