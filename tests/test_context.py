"""Context accounting and compaction.

At 64K the window is small enough that compaction runs often, so its failure
modes are ordinary rather than exotic: a history that opens on an orphaned tool
result, or one that silently forgets the task when the summariser fails.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur.config import Config
from wilbur.context import compact, estimate_tokens, _drop_orphan_tools


class Summariser:
    def __init__(self, summary="notes to self"):
        self.summary = summary
        self.calls = 0

    def chat(self, messages, schema, **kw):
        self.calls += 1
        if self.summary is None:
            raise RuntimeError("summariser unreachable")
        return type("R", (), {"content": self.summary})()


def history(n, *, role_cycle=("user", "assistant")):
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n):
        msgs.append({"role": role_cycle[i % len(role_cycle)],
                     "content": f"message {i} " + "x" * 400})
    return msgs


# --------------------------------------------------------------------- #
# estimate_tokens


def small_ctx() -> Config:
    """A window small enough that a toy history actually crosses the threshold."""
    cfg = Config()
    cfg.num_ctx = 4096
    return cfg


def test_estimate_counts_content():
    # 350 chars / 3.5 + 4 tokens of role-and-delimiter overhead
    assert estimate_tokens([{"role": "user", "content": "x" * 350}]) == 104


def test_estimate_counts_tool_call_payloads():
    bare = estimate_tokens([{"role": "assistant", "content": ""}])
    with_call = estimate_tokens([{
        "role": "assistant", "content": "",
        "tool_calls": [{"function": {"name": "grep", "arguments": {"pattern": "x"}}}],
    }])
    assert with_call > bare


def test_estimate_counts_the_tool_name_tag():
    """Tool results carry tool_name now; not counting it underestimates."""
    untagged = estimate_tokens([{"role": "tool", "content": "out"}])
    tagged = estimate_tokens([{"role": "tool", "tool_name": "read_file",
                               "content": "out"}])
    assert tagged > untagged


def test_estimate_survives_none_content():
    assert estimate_tokens([{"role": "assistant", "content": None}]) == 4


# --------------------------------------------------------------------- #
# orphan tool results


def test_drop_orphan_tools_strips_leading_results():
    msgs = [{"role": "tool", "content": "a"}, {"role": "tool", "content": "b"},
            {"role": "user", "content": "c"}]
    assert _drop_orphan_tools(msgs) == [{"role": "user", "content": "c"}]


def test_drop_orphan_tools_keeps_a_clean_history():
    msgs = [{"role": "user", "content": "c"}, {"role": "tool", "content": "a"}]
    assert _drop_orphan_tools(msgs) == msgs


def test_drop_orphan_tools_handles_all_tools():
    assert _drop_orphan_tools([{"role": "tool", "content": "a"}]) == []


# --------------------------------------------------------------------- #
# compact


def test_short_history_is_left_alone():
    msgs = history(4)
    assert compact(msgs, Summariser(), small_ctx()) is msgs


def test_compaction_preserves_the_system_prompt():
    out = compact(history(40), Summariser(), small_ctx())
    assert out[0]["role"] == "system"
    assert out[0]["content"] == "sys"


def test_compaction_inserts_a_marked_summary():
    out = compact(history(40), Summariser("what happened"), small_ctx())
    assert "[Earlier conversation, compacted]" in out[1]["content"]
    assert "what happened" in out[1]["content"]
    assert out[2]["role"] == "assistant"


def test_compaction_shrinks_the_history():
    msgs = history(40)
    out = compact(msgs, Summariser(), small_ctx())
    assert estimate_tokens(out) < estimate_tokens(msgs)


def test_failed_summariser_keeps_the_tail_instead_of_forgetting():
    msgs = history(40)
    out = compact(msgs, Summariser(summary=None), small_ctx())
    assert out[0]["role"] == "system"
    assert len(out) > 1
    assert out[-1] == msgs[-1]


def test_failed_summariser_does_not_open_on_an_orphan_tool_result():
    """The fallback path used to slice blindly and could strand a tool result."""
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(30):
        msgs.append({"role": "assistant", "content": "x" * 400,
                     "tool_calls": [{"function": {"name": "grep", "arguments": {}}}]})
        msgs.append({"role": "tool", "tool_name": "grep", "content": "y" * 400})
    out = compact(msgs, Summariser(summary=None), small_ctx())
    assert out[1]["role"] != "tool"


def test_empty_summary_is_treated_as_failure():
    out = compact(history(40), Summariser(summary="   "), small_ctx())
    assert "[Earlier conversation, compacted]" not in out[1].get("content", "")


def test_compaction_transcript_names_the_tool(monkeypatch):
    """A summary that says 'tool' rather than which tool is worth less."""
    captured = {}

    class Capturing(Summariser):
        def chat(self, messages, schema, **kw):
            captured["prompt"] = messages[-1]["content"]
            return super().chat(messages, schema, **kw)

    msgs = [{"role": "system", "content": "sys"}]
    for i in range(30):
        msgs.append({"role": "user", "content": "x" * 400})
        msgs.append({"role": "tool", "tool_name": "run_bash", "content": "y" * 400})
    compact(msgs, Capturing(), small_ctx())
    assert "TOOL:RUN_BASH" in captured["prompt"]
