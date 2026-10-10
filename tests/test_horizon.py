"""Run horizon, stuck detector, and the round-cap -> checkpoint -> continue
path (owner, 2026-10-02: "if I tell him to work all night, he should do it").
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wilbur.agent as agent_mod
from wilbur import horizon, progress
from wilbur.config import Config
from wilbur.llm import Reply
from wilbur.repl import Repl

from conftest import FakeClient, say, call


# --- horizon parsing ------------------------------------------------------- #

@pytest.mark.parametrize("text,kind,minutes", [
    ("work on it all night", "duration", 12 * 60),
    ("keep working overnight please", "duration", 12 * 60),
    ("for 3 hours", "duration", 180),
    ("for an hour", "duration", 60),
    ("for 90 minutes", "duration", 90),
    ("run for 2 hrs", "duration", 120),
    ("spend 45 min on this", "duration", 45),
    ("build the parser until it's done", "until_done", 12 * 60),
    ("don't stop until the tests pass", "until_done", 12 * 60),
    ("add a flag to the CLI", "until_done", 12 * 60),  # no horizon -> until done
])
def test_parse_horizon(text, kind, minutes):
    h = horizon.parse_horizon(text)
    assert h.kind == kind
    assert int(h.minutes) == minutes


def test_safety_max_bounds_all_night():
    h = horizon.parse_horizon("all night", safety_max_minutes=120)
    assert h.deadline_minutes == 12 * 60   # the stated intent
    assert h.minutes == 120                # but enforced is the safety cap


# --- stuck detector -------------------------------------------------------- #

def test_stuck_detector_fires_after_limit_no_progress_turns():
    d = horizon.StuckDetector(limit=3)
    # First turn is always progress (nothing to compare to).
    assert not d.update(files_changed=0, verifications=0, last_tool="grep(x)")
    assert not d.update(files_changed=0, verifications=0, last_tool="grep(x)")  # stale 1
    assert not d.update(files_changed=0, verifications=0, last_tool="grep(x)")  # stale 2
    assert d.update(files_changed=0, verifications=0, last_tool="grep(x)")      # stale 3
    assert "grep(x)" in d.report()


def test_a_file_change_resets_the_stuck_detector():
    d = horizon.StuckDetector(limit=2)
    assert not d.update(files_changed=0, verifications=0, last_tool="t")
    assert not d.update(files_changed=0, verifications=0, last_tool="t")  # stale 1
    assert not d.update(files_changed=1, verifications=0, last_tool="t")  # progress -> reset
    assert not d.update(files_changed=1, verifications=0, last_tool="t")  # stale 1
    assert d.update(files_changed=1, verifications=0, last_tool="t")      # stale 2


def test_a_new_error_counts_as_progress_then_repeat_is_stuck():
    d = horizon.StuckDetector(limit=2)
    assert not d.update(files_changed=0, verifications=0, last_error="E1")
    assert not d.update(files_changed=0, verifications=0, last_error="E2")  # changed -> reset
    assert not d.update(files_changed=0, verifications=0, last_error="E2")  # stale 1
    assert d.update(files_changed=0, verifications=0, last_error="E2")      # stale 2


# --- round cap -> checkpoint -> continue ----------------------------------- #

def _run(monkeypatch, tmp_path, replies, lines, configure=None):
    client = FakeClient(replies)
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    r_fd, w_fd = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(r_fd))
    os.close(w_fd)
    cfg = Config()
    cfg.checkpoint_commit_every = 0  # no git in a bare tmp dir
    if configure:
        configure(cfg)
    repl = Repl(cfg, str(tmp_path))
    repl.manager.available = lambda: True
    repl._use_ptk = False
    repl.approve_mode = "auto-edit"
    for line in lines:
        repl._input_q.put(line)
    t = threading.Thread(target=repl.run, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive()
    return repl, client


def _write(name):
    return Reply(content="", tool_calls=[call("write_file", path=name, content="x")])


def test_round_cap_checkpoints_and_carries_on(monkeypatch, tmp_path, capsys):
    # max_turns=2 forces the round cap. The first user message hits the cap
    # (two writes, no final reply), the agent returns a checkpoint summary, and
    # the keep-going loop carries on instead of stopping.
    replies = [
        _write("a.txt"), _write("b.txt"),            # fills max_turns=2 -> cap
        say("Checkpoint: wrote a and b, next write c."),  # the limit summary
        _write("c.txt"), say("All done, three files written."),
    ]

    def cfg(c):
        c.max_turns = 2

    repl, _ = _run(monkeypatch, tmp_path, replies, lines=("write a.txt, b.txt, c.txt",),
                   configure=cfg)
    out = capsys.readouterr().out
    assert "round cap reached" in out and "carrying on" in out
    assert (tmp_path / "c.txt").exists()
    # The progress log records the checkpoint.
    log = progress.log_path(repl.session_id)
    assert log.exists() and "round-cap checkpoint" in log.read_text()
