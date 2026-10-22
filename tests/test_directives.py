"""2.41.0 tool-use directives: every rule reaches the prompt, the prompt stays
inside its token budget, and a scripted (fake-backend) model that takes the
expensive road is steered to the right tool."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur import directives, persona
from wilbur.agent import SYSTEM_PROMPT, Agent
from wilbur.config import Config
from wilbur.context import CHARS_PER_TOKEN
from wilbur.tools import build_registry
from wilbur.tools.base import Approval

ALL_HOST = frozenset({"gate-run", "tb", "session-claim"})
# 2.40.1's system prompt (SYSTEM_PROMPT + persona block, wilbur voice) was
# 5,593 chars = 1,598 estimated tokens. The rules must fit within +15% of it.
BASELINE_CHARS = 5593


def _system(agent):
    return agent.messages[0]["content"]


def _agent(config, tmp_path, **kw):
    return Agent(config, str(tmp_path), approve=lambda *_: Approval.GRANTED, **kw)


def test_every_tool_rule_is_in_the_prompt(make_agent, monkeypatch):
    monkeypatch.setattr(directives, "host_commands", lambda: ALL_HOST)
    agent, _ = make_agent([])
    system = _system(agent)
    for _, rule in directives.TOOL_RULES:
        assert rule in system, rule
    for _, _, rule in directives._HOST_RULES:
        assert rule in system, rule
    for name in ("run_tests", "task", "list_skills", "gate-run --max-load 8", "tb <cmd>",
                 "session-claim check", "git worktree add"):
        assert name in system, name


def test_host_rules_absent_when_the_command_is_not_on_path(make_agent, monkeypatch):
    monkeypatch.setattr(directives, "host_commands", lambda: frozenset())
    agent, _ = make_agent([])
    system = _system(agent)
    assert "gate-run" not in system and "session-claim check" not in system
    assert "`tb <cmd>`" not in system


def test_restricted_agent_is_not_told_about_tools_it_lacks(tmp_path):
    agent = _agent(Config(), tmp_path, tool_names=["read_file", "grep", "glob"])
    system = _system(agent)
    assert "Find code -> grep" in system
    assert "run_tests" not in system and "list_skills" not in system


def test_owner_rules_kept_once_not_twice():
    block = SYSTEM_PROMPT + persona.system_extra("wilbur")
    assert block.count("Name exact targets") == 1
    assert block.count("`&&`, never `;`") == 1
    assert "Run the project's tests before every commit" in SYSTEM_PROMPT


def test_prompt_stays_inside_the_token_budget():
    tools = set(build_registry())
    full = (SYSTEM_PROMPT + "\n\n" + directives.tool_rules_for(tools, ALL_HOST)
            + "\n\n" + persona.system_extra("wilbur"))
    assert len(full) <= BASELINE_CHARS * 1.15, (len(full), BASELINE_CHARS)
    # It shrank: the duplicate owner rules paid for the new ones.
    assert len(full) < BASELINE_CHARS
    assert int(len(full) / CHARS_PER_TOKEN) < 1598


def test_host_probe_is_cached_so_the_prefix_cannot_change():
    assert directives.host_commands() is directives.host_commands()


# --- fake-backend scenarios: the scripted model is steered ------------------

def _bash(cmd):
    return {"tool_calls": [{"name": "run_bash", "arguments": {"command": cmd}}]}



def _run(fake_backend_config, tmp_path, script, **kw):
    config = fake_backend_config(script, **kw)
    agent = _agent(config, tmp_path)
    agent.run("do the job")
    return agent


def test_shell_grep_is_steered_to_the_grep_tool(fake_backend_config, tmp_path):
    (tmp_path / "a.py").write_text("needle = 1\n")
    agent = _run(fake_backend_config, tmp_path,
                 [_bash("grep -rn needle ."), _bash("grep -rn needle ."), "done"])
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert directives.HINTS["shell_search"] in results[0]
    # One-time: the second shell grep is not nagged again.
    assert directives.HINTS["shell_search"] not in results[1]


def test_bare_pytest_is_steered_to_run_tests(fake_backend_config, tmp_path):
    agent = _run(fake_backend_config, tmp_path, [_bash("true pytest -q"), "done"])
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert directives.HINTS["bare_tests"] in results[0]


def test_gate_run_test_command_is_not_nagged(fake_backend_config, tmp_path):
    agent = _run(fake_backend_config, tmp_path,
                 [_bash("true gate-run --max-load 8 pytest -q"), "done"])
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert directives.HINTS["bare_tests"] not in results[0]


def test_long_search_run_is_steered_to_task(fake_backend_config, tmp_path):
    (tmp_path / "a.py").write_text("x\n")
    script = [{"tool_calls": [{"name": "grep", "arguments": {"pattern": f"p{i}"}}]}
              for i in range(directives.SEARCH_RUN_FOR_TASK)] + ["done"]
    agent = _run(fake_backend_config, tmp_path, script, max_turns=20)
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert directives.HINTS["delegate"] in results[-1]
    assert not any(directives.HINTS["delegate"] in r for r in results[:-1])


def test_hints_can_be_switched_off(fake_backend_config, tmp_path):
    agent = _run(fake_backend_config, tmp_path,
                 [_bash("grep -rn x ."), "done"], tool_hints=False)
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "[harness] tip" not in results[0]


def test_hints_never_touch_the_system_prompt(fake_backend_config, tmp_path):
    config = fake_backend_config([_bash("grep -rn x ."), "done"])
    agent = _agent(config, tmp_path)
    before = _system(agent)
    agent.run("go")
    assert _system(agent) == before
