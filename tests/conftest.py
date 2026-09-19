"""Shared deterministic fake model backend for the whole test suite.

Every test that exercises the agent loop, tools, or sessions does so against
this fixture instead of a live Ollama model: no GPU, no network call, no
dependency on which model happens to be installed. `FakeClient` is scripted
with a queue of `Reply` objects (which may carry `tool_calls`, including more
than one per turn to script parallel tool calls, or a `task` call to script a
sub-agent) and stands in for `wilbur.llm.OllamaClient`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import wilbur.agent as agent_mod
import wilbur.config as config_mod
import wilbur.session as session_mod
from wilbur.agent import Agent
from wilbur.config import Config
from wilbur.llm import Reply, ToolCall
from wilbur.tools.base import Approval

try:
    import wilbur.__main__ as main_mod
except Exception:  # pragma: no cover -- __main__ may fail to import standalone
    main_mod = None

try:
    import wilbur.repl as repl_mod
except Exception:  # pragma: no cover
    repl_mod = None


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


# Captured at import time, before `isolate_wilbur_home` (or anything else)
# has a chance to monkeypatch config_mod.CONFIG_PATH -- this is the one
# owner-real path the guard below must prove untouched.
_REAL_CONFIG_PATH = config_mod.CONFIG_PATH
_REAL_HISTORY_PATH = config_mod.HISTORY_PATH


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
    yield
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


class FakeClient:
    """Returns queued replies, in order, and records every request it saw."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, *a, **kw):
        return self

    def chat(self, messages, schema, **kw):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else Reply(content="done", tool_calls=[])


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
