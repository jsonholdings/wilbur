"""Shared deterministic fake model backend for the whole test suite.

Every test that exercises the agent loop, tools, or sessions does so against
this fixture instead of a live Ollama model: no GPU, no network call, no
dependency on which model happens to be installed. `FakeClient`
(`wilbur/fake_backend.py`) is scripted with a queue of `Reply` objects (which
may carry `tool_calls`, including more than one per turn to script parallel
tool calls, or a `task` call to script a sub-agent) and stands in for
`wilbur.llm.OllamaClient`. `fake_backend_config` selects the same backend
through configuration instead (`backend: fake`), the way a scripted CLI run
does.
"""
import os
import signal
import sys
import threading
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Point every env-overridable Wilbur path at a throwaway directory BEFORE
# wilbur.config is imported, so the import-time defaults are never the
# owner's real ~/.local/share/wilbur or ~/.config/wilbur. The per-test
# `isolate_wilbur_home` monkeypatch alone was not enough: a repl thread that
# outlived its test (monkeypatch already undone) appended
# `test_idle_exit_fires_after_a_h0-*.progress.md` to the REAL sessions dir
# (2026-10-16), and subprocesses a test spawns inherit only the env. Forced,
# not setdefault, so a shell that exports the real paths cannot leak either.
import atexit
import shutil
import tempfile

_TEST_WILBUR_HOME = Path(tempfile.mkdtemp(prefix="wilbur-tests-"))
atexit.register(shutil.rmtree, _TEST_WILBUR_HOME, True)
os.environ["WILBUR_CONFIG"] = str(_TEST_WILBUR_HOME / "config.json")
os.environ["WILBUR_SESSIONS"] = str(_TEST_WILBUR_HOME / "sessions")
os.environ["WILBUR_HISTORY"] = str(_TEST_WILBUR_HOME / "history")
os.environ["WILBUR_USER_SKILLS_DIR"] = str(_TEST_WILBUR_HOME / "skills")
# Per-process GPU holder marks: a live Wilbur on this machine must not make release() think
# another holder is alive, and tests must never add or prune marks in the real dir.
os.environ["WILBUR_GPU_HOLDERS_DIR"] = str(_TEST_WILBUR_HOME / "gpu-holders")
# The owner's real sessions dir, computed independently of wilbur.config,
# for the session-wide guard below.
_REAL_SESSIONS_DIR = Path.home() / ".local" / "share" / "wilbur" / "sessions"

import pytest

import wilbur.agent as agent_mod
import wilbur.config as config_mod
import wilbur.fake_backend as fake_backend_mod
import wilbur.session as session_mod
import wilbur.verify as verify_mod
from wilbur.agent import Agent
from wilbur.config import Config
from wilbur.fake_backend import FakeClient, reset_shared_clients
from wilbur.llm import Reply, ToolCall
from wilbur.tools.base import Approval

pytest_plugins = ["pytester"]

try:
    import wilbur.__main__ as main_mod
except Exception:  # pragma: no cover -- __main__ may fail to import standalone
    main_mod = None

try:
    import wilbur.repl as repl_mod
except Exception:  # pragma: no cover
    repl_mod = None


# Module globals the guard watches: (label, module, attribute). A test that
# rebinds one without monkeypatch leaves every later test running against
# its replacement.
_GUARDED_GLOBALS = (
    ("wilbur.agent.OllamaClient", agent_mod, "OllamaClient"),
    ("wilbur.config.CONFIG_PATH", config_mod, "CONFIG_PATH"),
    ("wilbur.verify.detect", verify_mod, "detect"),
)
_IGNORED_ENV = ("PYTEST_CURRENT_TEST",)


def _shared_state():
    state = {
        "os.environ": {k: v for k, v in os.environ.items() if k not in _IGNORED_ENV},
        "cwd": os.getcwd(),
        "sys.stdin": sys.stdin,
        "SIGINT handler": signal.getsignal(signal.SIGINT),
    }
    for label, module, attr in _GUARDED_GLOBALS:
        state[label] = getattr(module, attr)
    state["wilbur.fake_backend._shared"] = dict(fake_backend_mod._shared)
    return state


def _restore_env(env: dict) -> None:
    for k in [k for k in os.environ if k not in env and k not in _IGNORED_ENV]:
        del os.environ[k]
    os.environ.update(env)


def _restore_shared(shared: dict) -> None:
    with fake_backend_mod._shared_lock:
        fake_backend_mod._shared.clear()
        fake_backend_mod._shared.update(shared)


def _set_stdin(value) -> None:
    sys.stdin = value


_RESTORERS = {
    "os.environ": _restore_env,
    "cwd": os.chdir,
    "sys.stdin": _set_stdin,
    "SIGINT handler": lambda h: signal.signal(signal.SIGINT, h),
    "wilbur.fake_backend._shared": _restore_shared,
    **{label: (lambda v, m=module, a=attr: setattr(m, a, v))
       for label, module, attr in _GUARDED_GLOBALS},
}


def _restore(before: dict, keys) -> None:
    """Put back each leaked piece of state recorded in `before`, so a leak
    fails only the test that caused it and never reaches the next test."""
    for key in keys:
        _RESTORERS[key](before[key])


class LeakedThreadWarning(UserWarning):
    """A test returned with a thread it started still running."""


def _new_threads(before: set) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t not in before and t.is_alive()]


@pytest.fixture(autouse=True)
def _no_leaked_state():
    """Fail the test that leaves process-wide state changed (env vars, cwd,
    stdin, a signal handler, a patched module global, a registered fake
    backend queue), after restoring that state, so the leak never breaks
    whichever test runs next. Defined first and using no fixture, so it is
    set up before and torn down after every monkeypatch -- it sees the state
    the next test will see (tests/test_leak_guard.py proves the ordering).

    Threads still running when the test returns (a stdin pump, an agent turn)
    cannot be undone from here, so they are reported as a
    `LeakedThreadWarning` instead of failing the test."""
    before = _shared_state()
    threads_before = set(threading.enumerate())
    yield
    leaked = _leaked(before, _shared_state())
    _restore(before, leaked)
    new = _new_threads(threads_before)
    if new:
        warnings.warn(LeakedThreadWarning(
            "test left threads running: " + ", ".join(
                f"{t.name}{' (daemon)' if t.daemon else ''}" for t in new)))
    assert not leaked, f"test leaked shared state: {', '.join(leaked)}"


def _leaked(before: dict, after: dict) -> list[str]:
    return [k for k in before if before[k] != after[k]]


@pytest.fixture(autouse=True)
def isolate_wilbur_home(monkeypatch, tmp_path):
    """No test may read or write the owner's real ~/.config/wilbur or
    ~/.local/share/wilbur. Every path the source code can write through
    (config, sessions, prompt_toolkit history) is redirected into tmp_path
    for every test, whether or not the test knows it touches one of them.

    Added after a real-config leak: a test picking a model via
    `wilbur.__main__._ensure_model` saved its fake pick ("other:1b") over
    the owner's live `~/.config/wilbur/config.json`, and a separate test
    driving the real `Repl._ptk_pump` wrote a real
    `~/.config/wilbur/history`. Individual tests patched CONFIG_PATH before;
    this fixture makes that the default instead of opt-in, since a new test
    reaching `Config.save()` or the ptk pump without knowing to patch it
    reintroduces the same leak.
    """
    fake_config = tmp_path / "config.json"
    fake_sessions = tmp_path / "sessions"
    fake_history = tmp_path / "history"
    fake_user_skills = tmp_path / "skills"

    monkeypatch.setattr(config_mod, "CONFIG_PATH", fake_config)
    monkeypatch.setattr(config_mod, "SESSION_DIR", fake_sessions)
    monkeypatch.setattr(config_mod, "HISTORY_PATH", fake_history)
    monkeypatch.setattr(config_mod, "USER_SKILLS_DIR", fake_user_skills)
    # Each of these modules imported the name directly (`from .config import
    # X`), which binds its own reference at import time -- patching the
    # source module above does not follow into an already-bound name.
    monkeypatch.setattr(session_mod, "SESSION_DIR", fake_sessions)
    if main_mod is not None and hasattr(main_mod, "CONFIG_PATH"):
        monkeypatch.setattr(main_mod, "CONFIG_PATH", fake_config)
    if repl_mod is not None and hasattr(repl_mod, "HISTORY_PATH"):
        monkeypatch.setattr(repl_mod, "HISTORY_PATH", fake_history)


# The owner-real paths the guard below must prove untouched. Computed from
# $HOME, not config_mod, because the env redirect above already points
# config_mod's import-time defaults at the throwaway directory.
_REAL_CONFIG_PATH = Path.home() / ".config" / "wilbur" / "config.json"
_REAL_HISTORY_PATH = Path.home() / ".config" / "wilbur" / "history"


def _test_named_sessions() -> set[str]:
    """Files in the REAL sessions dir whose name a test produced. A test's
    session id starts with its function name ("test_..."); the owner's live
    Wilbur may add its own files during the suite, and those are not ours."""
    try:
        return {p.name for p in _REAL_SESSIONS_DIR.iterdir() if p.name.startswith("test_")}
    except FileNotFoundError:
        return set()


def _snapshot(path: Path):
    if not path.exists():
        return None
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


@pytest.fixture(scope="session", autouse=True)
def guard_real_wilbur_home():
    """Session-wide control for `isolate_wilbur_home`: proves the real
    ~/.config/wilbur/config.json and history file are byte- and
    mtime-identical before and after the whole suite runs. A per-test
    monkeypatch fixture can itself be bypassed by a test that imports a
    path before the fixture patches it (as `test_configured_but_non_tool_
    capable_triggers_picker` did); this is the backstop that catches that
    class of regression even if a future test finds a new way around the
    per-test isolation.
    """
    before_config = _snapshot(_REAL_CONFIG_PATH)
    before_history = _snapshot(_REAL_HISTORY_PATH)
    before_sessions = _test_named_sessions()
    yield
    leaked_sessions = sorted(_test_named_sessions() - before_sessions)
    assert not leaked_sessions, (
        f"tests/conftest.py isolation failed: tests wrote {leaked_sessions} "
        f"into the real {_REAL_SESSIONS_DIR}"
    )
    after_config = _snapshot(_REAL_CONFIG_PATH)
    after_history = _snapshot(_REAL_HISTORY_PATH)
    assert after_config == before_config, (
        f"tests/conftest.py isolation failed: {_REAL_CONFIG_PATH} changed "
        f"during the suite ({before_config} -> {after_config})"
    )
    assert after_history == before_history, (
        f"tests/conftest.py isolation failed: {_REAL_HISTORY_PATH} changed "
        f"during the suite ({before_history} -> {after_history})"
    )


def say(text: str) -> Reply:
    return Reply(content=text, tool_calls=[])


def call(name: str, **args) -> ToolCall:
    return ToolCall(name=name, arguments=args)


@pytest.fixture
def fake_client_factory(monkeypatch):
    """Patches wilbur.agent.OllamaClient; returns a builder for a FakeClient."""
    def build(replies):
        client = FakeClient(replies)
        monkeypatch.setattr(agent_mod, "OllamaClient", client)
        return client
    return build


@pytest.fixture
def make_agent(monkeypatch, tmp_path):
    """Build an Agent wired to a scripted FakeClient. Returns (agent, client)."""
    def build(replies, **cfg_kw):
        client = FakeClient(replies)
        monkeypatch.setattr(agent_mod, "OllamaClient", client)
        config = Config()
        config.max_turns = cfg_kw.pop("max_turns", 10)
        approve = cfg_kw.pop("approve", lambda *_: Approval.GRANTED)
        for k, v in cfg_kw.items():
            setattr(config, k, v)
        a = Agent(config, str(tmp_path), approve=approve)
        return a, client
    return build


@pytest.fixture
def fake_backend_config(tmp_path):
    """Write `replies` as a fake-backend script and return a Config selecting
    it (`backend: fake`), with no monkeypatching: the Agent picks the client
    from configuration exactly as the CLI does."""
    import json

    def build(replies, **cfg_kw):
        script = tmp_path / "fake_script.json"
        script.write_text(json.dumps(replies))
        config = Config()
        config.backend = "fake"
        config.fake_script = str(script)
        for k, v in cfg_kw.items():
            setattr(config, k, v)
        return config
    return build


@pytest.fixture(autouse=True)
def _fresh_fake_backend(_no_leaked_state):
    """A script's shared queue (wilbur.fake_backend.shared_client) must not
    leak from one test into the next. Requests the guard so it is torn down
    before the guard checks the queue is empty again."""
    reset_shared_clients()
    yield
    reset_shared_clients()


@pytest.fixture(autouse=True)
def _no_live_gpu_queue(monkeypatch):
    """Never let a test reach the workstation's live gpu-queue (8791)."""
    monkeypatch.setenv("WILBUR_GPU_PRIORITY", "0")


@pytest.fixture
def release_stuck():
    """For tests that deliberately leave a worker stuck past a timeout: the
    stuck worker blocks on this Event instead of a fixed sleep, and teardown
    releases it and joins every thread the test started, so none outlives the
    test (a sleeping worker otherwise wakes mid-way through a later test)."""
    threads_before = set(threading.enumerate())
    release = threading.Event()
    yield release
    release.set()
    deadline = time.monotonic() + 10.0
    for t in _new_threads(threads_before):
        t.join(timeout=max(0.0, deadline - time.monotonic()))
    still = [t.name for t in _new_threads(threads_before)]
    assert not still, f"threads still running after release: {still}"
