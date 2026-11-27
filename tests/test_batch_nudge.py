"""R3 (2.66.0): fewer rounds. B0 (2026-11-26) measured 8.1 rounds per rep with
rounds ~= calls, so the model was not batching independent reads.

- an appended static-prompt line asks for independent reads/searches in one
  reply, and the 2.65.0 prompt stays a byte-identical prefix (prompt
  checkpoints: a tail rewrite re-reads ~40k tokens);
- a harness note is appended after 3 single-read rounds in a row, not after 2;
- a batched or a writing round resets the streak; `batch_nudge_after = 0` is off.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import call, say
from wilbur.agent import BATCH_NUDGE, SYSTEM_PROMPT
from wilbur.config import Config
from wilbur.llm import Reply

PROMPT_2650_LEN = 2229
PROMPT_2650_SHA256 = "8ed9fcad0f646c6622703c4b9e2313b73c75afea2cb76eade8c2e1b5edd7c978"


def _read_round(path: str) -> Reply:
    return Reply(content="", tool_calls=[call("read_file", path=path)])


def _nudges(a) -> list[dict]:
    marker = BATCH_NUDGE.split("{n}")[1][:30]
    return [m for m in a.messages if m.get("role") == "user" and marker in m["content"]]


def _files(tmp_path, n: int) -> None:
    for i in range(n):
        (tmp_path / f"f{i}.py").write_text(f"v{i} = {i}\n")


def test_prompt_prefix_bytes_unchanged_since_2650():
    prefix = SYSTEM_PROMPT[:PROMPT_2650_LEN].encode()
    assert hashlib.sha256(prefix).hexdigest() == PROMPT_2650_SHA256
    assert len(SYSTEM_PROMPT) > PROMPT_2650_LEN


def test_appended_line_asks_for_batched_reads_and_searches():
    tail = SYSTEM_PROMPT[PROMPT_2650_LEN:]
    assert "ONE reply" in tail and "reads" in tail and "greps" in tail


def test_nudge_fires_on_third_single_read_round_not_second(make_agent, tmp_path):
    _files(tmp_path, 3)
    events: list[str] = []
    a, _ = make_agent([_read_round("f0.py"), _read_round("f1.py"), _read_round("f2.py"),
                       say("done")])
    a.on_event = lambda kind, data: events.append(kind)
    assert a.run("read") == "done"
    nudges = _nudges(a)
    assert len(nudges) == 1
    # The note sits right after the third round's tool result.
    index = a.messages.index(nudges[0])
    assert a.messages[index - 1]["role"] == "tool"
    tool_results = [m for m in a.messages[:index] if m.get("role") == "tool"]
    assert len(tool_results) == 3
    assert events.count("batch_nudge") == 1


def test_two_single_read_rounds_do_not_nudge_control(make_agent, tmp_path):
    _files(tmp_path, 2)
    a, _ = make_agent([_read_round("f0.py"), _read_round("f1.py"), say("done")])
    a.run("read")
    assert _nudges(a) == []


def test_batched_round_resets_the_streak(make_agent, tmp_path):
    _files(tmp_path, 4)
    batched = Reply(content="", tool_calls=[call("read_file", path="f2.py"),
                                            call("read_file", path="f3.py")])
    a, _ = make_agent([_read_round("f0.py"), _read_round("f1.py"), batched,
                       _read_round("f0.py"), _read_round("f1.py"), say("done")])
    a.run("read")
    assert _nudges(a) == []


def test_write_round_resets_the_streak(make_agent, tmp_path):
    _files(tmp_path, 2)
    write = Reply(content="", tool_calls=[call("write_file", path="new.py",
                                               content="x = 1\n")])
    a, _ = make_agent([_read_round("f0.py"), _read_round("f1.py"), write,
                       _read_round("f0.py"), say("done")])
    a.run("go")
    assert _nudges(a) == []


def test_switch_off_sends_no_nudge(make_agent, tmp_path):
    _files(tmp_path, 3)
    a, _ = make_agent([_read_round("f0.py"), _read_round("f1.py"), _read_round("f2.py"),
                       say("done")], batch_nudge_after=0)
    a.run("read")
    assert _nudges(a) == []


def test_negative_threshold_clamps_to_off():
    cfg = Config()
    cfg.batch_nudge_after = -2
    cfg.validate()
    assert cfg.batch_nudge_after == 0


def test_streak_restarts_after_a_nudge(make_agent, tmp_path):
    """Six single-read rounds: a note after the 3rd and the 6th, not after 4 and 5."""
    _files(tmp_path, 6)
    a, _ = make_agent([_read_round(f"f{i}.py") for i in range(6)] + [say("done")])
    a.run("read")
    assert len(_nudges(a)) == 2
    marker = BATCH_NUDGE.split("{n}")[1][:30]
    at = [i for i, m in enumerate(a.messages)
          if m.get("role") == "user" and marker in m["content"]]
    assert sum(1 for m in a.messages[at[0]:at[1]] if m.get("role") == "tool") == 3
