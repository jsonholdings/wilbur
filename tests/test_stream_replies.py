"""U1a + U1b: live streamed replies.

U1a: the token stream reaches the display as `assistant_delta` events, and
`render.LiveReply` draws only complete lines, so a construct split anywhere
mid-stream renders exactly as the finished reply would.
U1b: the REPL streams on a tty when `stream_replies` is on, holds back while
the prompt owns the terminal, writes above the spinner, and does not print
the reply a second time once it was streamed whole.
"""
from __future__ import annotations

import json
import os
import pty
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import wilbur.agent as agent_mod
from wilbur import llm, render, ui
from wilbur.config import Config
from wilbur.llm import OllamaClient
from wilbur.repl import Repl

from conftest import FakeClient, call, say
from test_llm_stream_cap import Stream, _line

REPLY = ("Here is **the plan** for `wilbur/render.py`:\n\n"
         "| step | file |\n|---|---|\n| 1 | render.py |\n\n"
         "```python\ndef f(x):\n    return x * 2\n```\n\n"
         "- one\n- two with *emphasis*\nDone.")


# ------------------------------------------------------------------ U1a

def test_live_reply_any_split_renders_like_the_finished_reply(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", False)
    whole = render.markdown(REPLY) + "\n"
    for cut in range(1, len(REPLY)):
        live = render.LiveReply()
        got = live.feed(REPLY[:cut]) + live.feed(REPLY[cut:]) + live.close()
        assert got == whole, f"split at {cut}: {REPLY[cut - 5:cut + 5]!r}"


def test_live_reply_char_by_char_and_leading_blank_dropped(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", False)
    live = render.LiveReply()
    got = "".join(live.feed(ch) for ch in "\n\n  " + REPLY) + live.close()
    assert got == render.markdown(REPLY) + "\n"


def test_live_reply_holds_a_split_bold_run_until_the_line_ends(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", False)
    live = render.LiveReply()
    assert live.feed("a **bo") == ""          # nothing half-drawn
    out = live.feed("ld** b\n")
    assert "**" not in out and "bold" in out and ui.BOLD in out


def test_live_reply_colour_off_passes_pieces_raw(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", True)
    live = render.LiveReply()
    assert live.feed("\n a **b") == "a **b"
    assert live.feed("**") == "**"
    assert live.close() == "\n"


def test_control_live_reply_with_no_text_emits_nothing(monkeypatch):
    monkeypatch.setattr(ui, "_NO_COLOR", False)
    live = render.LiveReply()
    assert live.feed("  \n") == "" and live.close() == ""


def test_ollama_client_hands_each_content_piece_to_on_content(monkeypatch):
    lines = [(0.0, _line({"message": {"thinking": "hm"}, "done": False})),
             (0.0, _line({"message": {"content": "ab"}, "done": False})),
             (0.0, _line({"message": {"content": "cd"}, "done": True}))]
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda request, timeout=None: Stream(lines))
    client = OllamaClient("http://x", "m", max_retries=0)
    seen: list[str] = []
    client.on_content = seen.append
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert seen == ["ab", "cd"] and reply.content == "abcd"


def test_agent_emits_deltas_before_the_assistant_event(make_agent):
    agent, _ = make_agent([say("hello **world**, this is long enough")])
    events: list[tuple[str, dict]] = []
    agent.on_event = lambda kind, data: events.append((kind, data))
    agent.run("hi")
    kinds = [k for k, _ in events]
    deltas = [d["text"] for k, d in events if k == "assistant_delta"]
    assert len(deltas) > 1, "one piece only: the split path never ran"
    assert "".join(deltas) == "hello **world**, this is long enough"
    assert kinds.index("assistant") > max(i for i, k in enumerate(kinds) if k == "assistant_delta")


def test_control_tool_only_reply_emits_no_delta(make_agent):
    agent, _ = make_agent([Reply_tool_only(), say("ok")])
    events: list[str] = []
    agent.on_event = lambda kind, data: events.append(kind if kind != "assistant_delta"
                                                      else "delta:" + data["text"])
    agent.run("hi")
    first_assistant = events.index("assistant")
    assert not any(e.startswith("delta:") for e in events[:first_assistant])


def Reply_tool_only():
    from wilbur.llm import Reply
    return Reply(content="", tool_calls=[call("list_files", path=".")])


# ------------------------------------------------------------------ U1b

def _repl(monkeypatch, tmp_path, replies, *, tty=True, stream=True):
    client = FakeClient(replies)
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    cfg = Config()
    cfg.stream_replies = stream
    repl = Repl(cfg, str(tmp_path))
    monkeypatch.setattr(repl, "_tty", lambda: tty)
    return repl


def _turn(repl, text):
    repl._live_begin()
    try:
        reply = repl.agent.run(text)
    finally:
        repl._live_end()
    repl._print_reply(reply)
    return reply


def test_tty_streams_once_and_does_not_reprint(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ui, "_NO_COLOR", False)
    repl = _repl(monkeypatch, tmp_path, [say(REPLY)])
    capsys.readouterr()
    _turn(repl, "hi")
    out = capsys.readouterr().out
    assert out == "\n" + render.markdown(REPLY) + "\n"
    assert out.count("the plan") == 1


def test_control_stream_replies_off_prints_the_finished_reply(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ui, "_NO_COLOR", False)
    repl = _repl(monkeypatch, tmp_path, [say(REPLY)], stream=False)
    capsys.readouterr()
    _turn(repl, "hi")
    assert capsys.readouterr().out == "\n" + ui.assistant(REPLY) + "\n"


def test_non_tty_never_streams(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ui, "_NO_COLOR", True)
    repl = _repl(monkeypatch, tmp_path, [say(REPLY)], tty=False)
    capsys.readouterr()
    _turn(repl, "hi")
    assert capsys.readouterr().out == "\n" + REPLY + "\n"


def test_reply_changed_after_streaming_is_printed(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ui, "_NO_COLOR", True)
    repl = _repl(monkeypatch, tmp_path, [say("first")])
    capsys.readouterr()
    repl._live_begin()
    reply = repl.agent.run("hi")
    repl._live_end()
    repl._print_reply(reply + " and more")
    out = capsys.readouterr().out
    assert out.count("first") == 2


def test_held_while_approval_owns_the_terminal(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ui, "_NO_COLOR", True)
    repl = _repl(monkeypatch, tmp_path, [])
    capsys.readouterr()
    repl._live_begin()
    repl._awaiting_approval.set()
    repl._on_event("assistant_delta", {"text": "hello\n"})
    assert capsys.readouterr().out == ""
    repl._awaiting_approval.clear()
    repl._on_event("assistant_delta", {"text": "there"})
    assert capsys.readouterr().out == "\nhello\nthere"
    repl._on_event("assistant", {"content": "hello\nthere"})
    repl._live_end()
    assert capsys.readouterr().out == "\n"


def test_held_while_the_user_types_then_released_at_turn_end(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ui, "_NO_COLOR", True)
    repl = _repl(monkeypatch, tmp_path, [])
    spinner = ui.Spinner(render=False)
    repl._spinner = spinner
    capsys.readouterr()
    repl._live_begin()
    spinner.pause()
    repl._on_event("assistant_delta", {"text": "typed over?"})
    repl._on_event("assistant", {"content": "typed over?"})
    assert capsys.readouterr().out == ""
    repl._live_end()
    assert capsys.readouterr().out == "\ntyped over?\n"
    repl._spinner = None


def test_pty_streamed_lines_are_whole_between_spinner_redraws(monkeypatch, tmp_path):
    """A real pty with the spinner drawing: each streamed line is written in
    one piece after a line clear, never cut by a spinner frame."""
    monkeypatch.setattr(ui, "_NO_COLOR", False)
    controller_fd, follower_fd = pty.openpty()
    follower_out = os.fdopen(follower_fd, "w", buffering=1)
    monkeypatch.setattr(sys, "stdout", follower_out)
    repl = _repl(monkeypatch, tmp_path, [say(REPLY)])
    got = bytearray()
    done = threading.Event()

    def drain():
        import select
        while not done.is_set():
            r, _, _ = select.select([controller_fd], [], [], 0.05)
            if r:
                try:
                    got.extend(os.read(controller_fd, 65536))
                except OSError:
                    return

    reader = threading.Thread(target=drain)
    reader.start()
    try:
        spinner = ui.Spinner()
        repl._spinner = spinner
        repl._live_begin()
        with spinner:
            time.sleep(0.3)
            for i in range(0, len(REPLY), 3):
                repl._on_event("assistant_delta", {"text": REPLY[i:i + 3]})
                time.sleep(0.01)
            repl._on_event("assistant", {"content": REPLY})
            time.sleep(0.3)
        repl._live_end()
        repl._spinner = None
        time.sleep(0.2)
    finally:
        done.set()
        reader.join(2)
        follower_out.close()
        os.close(controller_fd)
    text = bytes(got).decode("utf-8", "replace").replace("\r\n", "\n")
    for line in render.markdown(REPLY).split("\n"):
        if line.strip():
            assert line in text, f"streamed line cut or missing: {line!r}"
    assert "⠋" in text or any(g in text for g in ui.SPINNER), "spinner never drew"


def test_streamed_copy_is_consumed_by_the_first_print(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ui, "_NO_COLOR", True)
    repl = _repl(monkeypatch, tmp_path, [say("same")])
    capsys.readouterr()
    _turn(repl, "hi")
    capsys.readouterr()
    repl._print_reply("same")          # a later loop reply with the same text
    assert capsys.readouterr().out == "\nsame\n"
