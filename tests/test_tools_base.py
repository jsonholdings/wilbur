"""Unit tests for wilbur/tools/base.py: Approval denial messages and clamp().

Covers the two pure functions in the tool contract module:

* `denial_message` -- every Approval member other than GRANTED must yield a
  distinct message, so the model can never read "interrupted" or
  "unavailable" as "the user denied it" (or as "it ran").
* `clamp` -- short text passes through unchanged; text over `max_chars` or
  over `max_lines` is cut, keeping head and tail and stating what was removed.

Pure functions only: no network, no GPU, no filesystem, no real
~/.config/wilbur or ~/.local (the conftest `isolate_wilbur_home` fixture
still applies as a backstop).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wilbur.tools.base import Approval, clamp, denial_message


# ------------------------------------------------------- denial_message --

def test_every_non_granted_outcome_has_a_distinct_message():
    non_granted = [o for o in Approval if o is not Approval.GRANTED]
    assert len(non_granted) >= 2, "expected at least two non-GRANTED outcomes"
    messages = {o: denial_message("command", o) for o in non_granted}
    texts = list(messages.values())
    assert len(set(texts)) == len(texts), (
        f"non-GRANTED outcomes must produce distinct messages, got: {messages}"
    )


def test_denial_messages_all_state_the_call_did_not_run():
    for outcome in Approval:
        if outcome is Approval.GRANTED:
            continue
        msg = denial_message("bash command", outcome)
        assert "did not run" in msg or "did NOT run" in msg, (
            f"{outcome.value} message must say the call did not run: {msg!r}"
        )


def test_denial_message_names_the_thing():
    msg = denial_message("write_file", Approval.DENIED)
    assert "write_file" in msg


# ------------------------------------------------------------------ clamp --

def test_clamp_leaves_short_text_unchanged():
    text = "line one\nline two\nline three"
    assert clamp(text, max_chars=100, max_lines=10) == text


def test_clamp_cuts_text_over_max_chars():
    text = "x" * 500  # single line, so only the char limit can fire
    out = clamp(text, max_chars=100, max_lines=10)
    assert "characters omitted" in out
    assert len(out) < len(text)
    # head and tail of the original are preserved
    assert out.startswith("x" * 50)
    assert out.endswith("x" * 50)


def test_clamp_cuts_text_over_max_lines():
    text = "\n".join(f"line {i}" for i in range(30))
    out = clamp(text, max_chars=10_000, max_lines=6)
    assert "lines omitted" in out
    assert len(out.splitlines()) < len(text.splitlines())
    # head and tail lines survive
    assert out.splitlines()[0] == "line 0"
    assert out.splitlines()[-1] == "line 29"
    # the marker states how many lines were removed: 30 - 4 head - 2 tail
    assert "[24 lines omitted]" in out
