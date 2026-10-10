"""Orphan tool results and token accounting in wilbur/context.py.

Two focused checks: a history that opens on a tool result whose assistant
call is gone must not be handed to the model, and the token estimate must
grow as the history grows.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur.context import _drop_orphan_tools, estimate_tokens


def test_drop_orphan_tools_removes_orphan_keeps_matched_pair():
    orphan = {"role": "tool", "tool_name": "grep", "content": "stale output"}
    call = {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": "grep", "arguments": {}}}]
            }
    result = {"role": "tool", "tool_name": "grep", "content": "fresh output"}
    # The leading tool result has no assistant call before it: dropped.
    assert _drop_orphan_tools([orphan, call, result]) == [call, result]
    # A tool result that follows its assistant call is a matched pair: kept.
    assert _drop_orphan_tools([call, result]) == [call, result]


def test_estimate_tokens_grows_when_a_message_is_added():
    base = [{"role": "user", "content": "hello"}]
    added = base + [{"role": "assistant", "content": "world"}]
    assert estimate_tokens(added) > estimate_tokens(base)
