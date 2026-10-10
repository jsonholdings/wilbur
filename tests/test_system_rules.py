"""The owner's portable global rules are baked into every agent's system prompt."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.conftest import say


def test_every_agent_carries_the_safety_rules(make_agent):
    agent, client = make_agent([say("done")])
    agent.run("hi")
    system = client.seen[0][0]["content"]
    for rule in ("Name exact targets", "git add -A", "`&&`, never `;`",
                 "Never print, log or commit a secret", "verified", "bump the version",
                 "Never run sudo, su, doas or pkexec"):
        assert rule in system, rule


def test_team_roles_inherit_the_safety_rules(tmp_path, fake_client_factory):
    from wilbur.config import Config
    from wilbur.team import TeamOrchestrator
    from wilbur.tools.base import Approval

    client = fake_client_factory([say("plan")])
    orch = TeamOrchestrator(Config(), str(tmp_path), approve=lambda *_: Approval.GRANTED)
    orch.plan("do x")
    system = client.seen[0][0]["content"]
    assert "Name exact targets" in system and "You are the PLANNER" in system
