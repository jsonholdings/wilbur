"""The deterministic fake model backend (wilbur/fake_backend.py).

Covers the client itself, how it is selected (configuration or a fixture,
never silently), and full runs through the real Agent, the real Repl and the
real CLI with no model, no Ollama and no network.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.agent as agent_mod
from wilbur import fake_backend
from wilbur.__main__ import main
from wilbur.agent import Agent
from wilbur.config import Config
from wilbur.fake_backend import FakeBackendError, FakeClient, check_backend, make_client
from wilbur.llm import OllamaClient, Reply
from wilbur.repl import Repl
from wilbur.tools.base import Approval

from conftest import call

REPO = Path(__file__).resolve().parents[1]


def _forbid_ollama(monkeypatch):
    """Any attempt to build the real client fails the test."""
    def boom(*a, **kw):
        raise AssertionError("OllamaClient constructed under the fake backend")
    monkeypatch.setattr(agent_mod, "OllamaClient", boom)


def _script(tmp_path, data, name="script.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return str(p)


# --- the client ----------------------------------------------------------- #

def test_reply_objects_come_back_verbatim_and_requests_are_recorded():
    r = Reply(content="hi", tool_calls=[call("read_file", path="a")])
    client = FakeClient([r])
    assert client.chat([{"role": "user", "content": "x"}], [{"t": 1}], num_ctx=8) is r
    assert client.seen == [[{"role": "user", "content": "x"}]]
    assert client.requests[0]["tools"] == [{"t": 1}]
    assert client.requests[0]["options"] == {"num_ctx": 8}


def test_exhausted_script_returns_a_plain_reply():
    assert FakeClient([]).chat([]).content == "done"
    reply = FakeClient([], exhausted="out of script").chat([])
    assert reply.content == "out of script" and reply.tool_calls == []


def test_script_turns_build_tool_calls():
    client = FakeClient([{"content": "", "thinking": "hmm",
                          "tool_calls": [{"name": "glob", "arguments": {"pattern": "*"}}]}])
    reply = client.chat([], known_tools={"glob"})
    assert reply.thinking == "hmm"
    assert [(c.name, c.arguments, c.recovered) for c in reply.tool_calls] == [
        ("glob", {"pattern": "*"}, False)]


def test_text_tool_call_is_recovered_like_the_real_client():
    text = '<tool_call>{"name": "read_file", "arguments": {"path": "a.txt"}}</tool_call>'
    reply = FakeClient([text]).chat([], known_tools={"read_file"})
    assert [(c.name, c.arguments, c.recovered) for c in reply.tool_calls] == [
        ("read_file", {"path": "a.txt"}, True)]
    # No known tools -> no recovery, exactly as OllamaClient.chat behaves.
    assert FakeClient([text]).chat([]).tool_calls == []


def test_from_script_accepts_a_list_or_an_object(tmp_path):
    c = FakeClient.from_script(_script(tmp_path, ["one", {"content": "two"}]))
    assert [c.chat([]).content for _ in range(3)] == ["one", "two", "done"]
    c = FakeClient.from_script(_script(tmp_path, {"replies": ["a"], "exhausted": "fin"}, "o.json"))
    assert [c.chat([]).content for _ in range(2)] == ["a", "fin"]


@pytest.mark.parametrize("body, match", [
    (None, "not found"),
    ("{not json", "not valid JSON"),
    ('{"replies": 3}', "must be a list"),
    ("[42]", "string or object"),
    ('[{"tool_calls": [{"arguments": {}}]}]', "string 'name'"),
    ('[{"tool_calls": [{"name": "x", "arguments": []}]}]', "must be an object"),
    # PR #2 review finding 4: these loaded, then failed partway through a run.
    ('{"replies": [], "exhausted": null}', "string or object"),
    ('{"replies": [], "exhausted": {"content": 5}}', "'content' must be a string"),
    ('[{"content": 5}]', "'content' must be a string"),
    ('[{"content": "x", "thinking": ["no"]}]', "'thinking' must be a string"),
])
def test_bad_scripts_fail_on_load(tmp_path, body, match):
    p = tmp_path / "bad.json"
    if body is not None:
        p.write_text(body)
    with pytest.raises(FakeBackendError, match=match):
        FakeClient.from_script(p)


# --- selection: explicit only --------------------------------------------- #

def test_default_backend_is_ollama():
    cfg = Config()
    assert cfg.backend == "ollama" and check_backend(cfg) == "ollama"


def test_ollama_backend_never_uses_the_script_even_if_one_is_set(tmp_path):
    cfg = Config()
    cfg.fake_script = _script(tmp_path, ["x"])
    assert isinstance(make_client(cfg, "m"), OllamaClient)


def test_a_session_never_saves_the_backend_into_config():
    import wilbur.config as config_mod
    cfg = Config()
    cfg.backend, cfg.fake_script = "fake", "/tmp/s.json"
    cfg.save()
    saved = json.loads(config_mod.CONFIG_PATH.read_text())
    assert "backend" not in saved and "fake_script" not in saved
    assert Config.load().backend == "ollama"
    # A backend the user wrote by hand is kept as written.
    config_mod.CONFIG_PATH.write_text(json.dumps({"backend": "fake", "fake_script": "x.json"}))
    cfg = Config.load()
    cfg.backend, cfg.num_ctx = "ollama", 4096
    cfg.save()
    saved = json.loads(config_mod.CONFIG_PATH.read_text())
    assert (saved["backend"], saved["fake_script"], saved["num_ctx"]) == ("fake", "x.json", 4096)


def test_unknown_backend_is_an_error():
    cfg = Config()
    cfg.backend = "fakee"
    with pytest.raises(FakeBackendError, match="unknown backend"):
        check_backend(cfg)


def test_fake_backend_without_a_script_is_an_error():
    cfg = Config()
    cfg.backend = "fake"
    with pytest.raises(FakeBackendError, match="needs a script"):
        make_client(cfg, "m")


def test_agents_on_one_script_share_one_queue(fake_backend_config, tmp_path):
    cfg = fake_backend_config(["first", "second"])
    a = Agent(cfg, str(tmp_path), approve=lambda *_: Approval.GRANTED)
    b = Agent(cfg, str(tmp_path), approve=lambda *_: Approval.GRANTED)
    assert a.client is b.client
    assert a.run("go") == "first"
    assert b.run("go") == "second"


# --- full runs with no model ---------------------------------------------- #

def test_agent_runs_a_tool_turn_from_configuration(monkeypatch, fake_backend_config, tmp_path):
    _forbid_ollama(monkeypatch)
    (tmp_path / "a.txt").write_text("hello from a\n")
    cfg = fake_backend_config([
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]},
        {"tool_calls": [{"name": "write_file",
                         "arguments": {"path": "b.txt", "content": "copied\n"}}]},
        "Read a.txt and wrote b.txt.",
    ])
    agent = Agent(cfg, str(tmp_path), approve=lambda *_: Approval.GRANTED)
    assert agent.run("copy it") == "Read a.txt and wrote b.txt."
    assert (tmp_path / "b.txt").read_text() == "copied\n"
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert "hello from a" in tool_msgs[0]["content"]


def test_subagent_reads_the_same_script(monkeypatch, fake_backend_config, tmp_path):
    _forbid_ollama(monkeypatch)
    cfg = fake_backend_config([
        {"tool_calls": [{"name": "task", "arguments": {
            "description": "look", "prompt": "list the files", "agent_type": "explore"}}]},
        "subagent report: nothing here",
        "Parent done.",
    ])
    agent = Agent(cfg, str(tmp_path), approve=lambda *_: Approval.GRANTED)
    assert agent.run("delegate") == "Parent done."
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert "subagent report: nothing here" in tool_msgs[0]["content"]


def test_repl_runs_on_the_fake_backend_without_ollama(monkeypatch, fake_backend_config,
                                                      tmp_path, capsys, request):
    _forbid_ollama(monkeypatch)
    cfg = fake_backend_config(["Howdy."])
    r_fd, w_fd = os.pipe()
    stdin = os.fdopen(r_fd)
    request.addfinalizer(stdin.close)  # the pipe must not outlive the test
    monkeypatch.setattr(sys, "stdin", stdin)
    os.close(w_fd)
    repl = Repl(cfg, str(tmp_path))

    def no_ollama(*a, **kw):
        raise AssertionError("Repl asked Ollama under the fake backend")
    repl.manager.available = no_ollama
    repl.manager.list_models = no_ollama
    repl._use_ptk = False
    repl._input_q.put("hello")
    t = threading.Thread(target=repl.run, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive()
    assert "Howdy." in capsys.readouterr().out


class _GpuPriorityStub:
    """Stands in for the private build's gpu_priority module."""
    def __init__(self):
        self.loaded = 0

    def load(self):
        self.loaded += 1
        return self


def test_repl_skips_the_gpu_queue_on_the_fake_backend(monkeypatch, fake_backend_config, tmp_path):
    """PR #2 review finding 5: a scripted REPL turn must never queue for the
    GPU (gpu-queue, port 8791) -- the fake backend runs no model."""
    import wilbur.repl as repl_mod
    stub = _GpuPriorityStub()
    monkeypatch.setattr(repl_mod, "_gpu_priority", stub)
    repl = Repl(fake_backend_config(["Howdy."]), str(tmp_path))
    assert repl._gp is None and stub.loaded == 0
    assert repl._gpu_gate("hello") is True
    assert Repl(Config(), str(tmp_path))._gp is stub  # ollama still queues


def test_cli_oneshot_on_the_fake_backend_with_no_network(tmp_path):
    """The real `wilbur -p` in a subprocess, base_url at a dead port: the
    run succeeds only if nothing tries to reach a model server."""
    work = tmp_path / "work"
    work.mkdir()
    script = _script(tmp_path, [
        {"tool_calls": [{"name": "write_file",
                         "arguments": {"path": "out.txt", "content": "made by script\n"}}]},
        "Wrote out.txt.",
    ])
    env = dict(os.environ, WILBUR_CONFIG=str(tmp_path / "config.json"),
               WILBUR_SESSIONS=str(tmp_path / "sessions"),
               WILBUR_HISTORY=str(tmp_path / "history"),
               WILBUR_USER_SKILLS_DIR=str(tmp_path / "skills"),
               WILBUR_GPU_PRIORITY="0", NO_COLOR="1")
    proc = subprocess.run(
        [sys.executable, "-m", "wilbur", "-p", "write the file", "-C", str(work),
         "--url", "http://127.0.0.1:9", "--backend", "fake", "--fake-script", script],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "Wrote out.txt." in proc.stdout
    assert "FAKE BACKEND" in proc.stderr
    assert (work / "out.txt").read_text() == "made by script\n"


def test_cli_refuses_fake_backend_without_a_script(capsys):
    assert main(["--backend", "fake", "-p", "hi"]) == 2
    assert "needs a script" in capsys.readouterr().err


def test_cli_refuses_an_unknown_backend_in_config(tmp_path, capsys):
    import wilbur.config as config_mod
    config_mod.CONFIG_PATH.write_text(json.dumps({"backend": "nope"}))
    assert main(["-p", "hi"]) == 2
    assert "unknown backend" in capsys.readouterr().err


def test_cli_list_models_on_fake_backend_touches_no_server(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("wilbur.__main__._list_models",
                        lambda c: (_ for _ in ()).throw(AssertionError("asked Ollama")))
    script = _script(tmp_path, [])
    assert main(["--backend", "fake", "--fake-script", script, "--list-models"]) == 0
    assert "fake backend" in capsys.readouterr().out


def test_shared_queue_is_reset_between_tests_by_the_fixture(tmp_path):
    assert fake_backend._shared == {}
