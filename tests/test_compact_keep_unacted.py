"""R2: compaction keeps reads the model has not acted on (compact_keep_unacted).

Live W3 run A, 2026-11-26: twelve ~4.7k-token modules at num_ctx 32768. Reads
were compacted to a prose summary before the model edited those files, so it
read them again (2.61.0: 7 compactions, 96 reads, 0 edits; 2.62.0 holds reads
back but still summarised unacted ones: 29 reads for 12 modules). A policy model
reproduces that: it edits a module only while its contents are visible in the
prompt, and otherwise reads every unfixed module in one round.
"""
import re
from collections import Counter

from wilbur import agent as agent_mod
from wilbur.agent import Agent
from wilbur.config import Config
from wilbur import context as context_mod
from wilbur.context import compact
from wilbur.llm import Reply, ToolCall
from wilbur.tools.base import Approval

NUM_CTX = 32_768
N_MODULES = 12
GATHER = 4  # live A: the model read several modules before editing any


class PolicyClient:
    def __init__(self, root):
        self.root = root
        self.reads = Counter()  # module contents actually delivered to the model
        self.compactions = 0
        self.calls = 0

    def __call__(self, *a, **kw):
        return self

    def chat(self, messages, tools=None, *, known_tools=None, **options):
        self.calls += 1
        if messages and messages[0].get("content") == "You compact agent transcripts.":
            self.compactions += 1
            return Reply(content="Notes: fixing a bug in each module; some are done.", tool_calls=[])
        last_assistant = max((k for k, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
        for m in messages[last_assistant + 1:]:
            if m.get("role") == "tool":
                for i in re.findall(r"\t# module (\d+)\n", m.get("content") or ""):
                    self.reads[int(i)] += 1
        visible = set()
        for m in messages:
            visible.update(int(i) for i in re.findall(r"\t# module (\d+)\n", m.get("content") or ""))
        unfixed = [i for i in range(N_MODULES)
                   if "FIXED" not in (self.root / f"mod{i:02d}.py").read_text()]
        if not unfixed:
            return Reply(content="all modules fixed", tool_calls=[])
        seen_unfixed = [i for i in unfixed if i in visible]
        unseen = [i for i in unfixed if i not in visible]
        if unseen and len(seen_unfixed) < min(GATHER, len(unfixed)):
            # gathers context before editing, like the live model did
            return Reply(content="", tool_calls=[ToolCall(name="read_file", arguments={"path": f"mod{i:02d}.py"})
                                                 for i in unseen])
        for i in unfixed:
            if i in visible:
                return Reply(content="", tool_calls=[ToolCall(name="edit_file", arguments={
                    "path": f"mod{i:02d}.py", "old_string": f"# module {i}\n",
                    "new_string": f"# module {i} FIXED\n"})])
        return Reply(content="", tool_calls=[ToolCall(name="read_file", arguments={"path": f"mod{i:02d}.py"})
                                             for i in unfixed])


def _run(monkeypatch, tmp_path, keep_unacted):
    for i in range(N_MODULES):
        (tmp_path / f"mod{i:02d}.py").write_text(f"# module {i}\n" + ("x = 1\n" * (16_000 // 6)))
    client = PolicyClient(tmp_path)
    monkeypatch.setattr(agent_mod, "OllamaClient", client)
    config = Config()
    config.num_ctx = NUM_CTX
    config.max_turns = 200
    config.compact_keep_unacted = keep_unacted
    a = Agent(config, str(tmp_path), approve=lambda *_: Approval.GRANTED)
    a.run("fix the bug in each module")
    fixed = sum("FIXED" in (tmp_path / f"mod{i:02d}.py").read_text() for i in range(N_MODULES))
    return client, fixed


def test_unacted_reads_survive_compaction_so_each_module_is_reread_at_most_once(monkeypatch, tmp_path):
    client, fixed = _run(monkeypatch, tmp_path, keep_unacted=True)
    assert fixed == N_MODULES
    assert client.compactions > 0  # the budget really forced compaction
    assert set(client.reads) == set(range(N_MODULES))
    rereads = {i: n - 1 for i, n in client.reads.items() if n > 1}
    assert all(n <= 1 for n in rereads.values()), f"re-read more than once: {rereads}"


def test_control_old_policy_rereads_modules_it_had_not_acted_on(monkeypatch, tmp_path):
    client, _ = _run(monkeypatch, tmp_path, keep_unacted=False)
    assert client.compactions > 0
    assert max(client.reads.values()) > 2, f"old policy should thrash: {dict(client.reads)}"


def _read_round(paths, start=0):
    msgs = [{"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": p}}} for p in paths]}]
    for n, p in enumerate(paths):
        msgs.append({"role": "tool", "tool_name": "read_file",
                     "content": "\n".join(f"{k:6d}\tline {p} {k}" for k in range(1, 40))})
    return msgs


class _Summ:
    def __init__(self):
        self.seen = []

    def chat(self, messages, tools=None, **kw):
        self.seen.append(messages[-1]["content"])
        return Reply(content="summary", tool_calls=[])


def _history():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    msgs += _read_round(["a.py", "b.py"])
    msgs += [{"role": "assistant", "content": "",
              "tool_calls": [{"function": {"name": "edit_file", "arguments": {"path": "a.py"}}}]},
             {"role": "tool", "tool_name": "edit_file", "content": "ok"}]
    for k in range(6):
        msgs += [{"role": "user", "content": f"filler {k} " + "y" * 4000},
                 {"role": "assistant", "content": f"ack {k}"}]
    msgs += _read_round(["c.py", "d.py", "e.py"])  # the newest round
    return msgs


def test_newest_round_is_kept_verbatim_and_acted_read_is_summarised():
    config = Config()
    config.num_ctx = 8_192
    config.compact_keep_unacted = True
    out = compact(_history(), _Summ(), config)
    text = "\n".join(m.get("content") or "" for m in out)
    tools = [m for m in out if m.get("role") == "tool"]
    assert len(tools) == 3 and "line e.py 39" in text  # newest round intact
    assert "line b.py 1" in text  # unacted read carried (or indexed below)
    assert "line a.py 1" not in text  # a.py was edited: summarised, not carried


def test_unacted_reads_past_the_share_become_a_file_index(monkeypatch):
    monkeypatch.setattr(context_mod, "UNACTED_KEEP_SHARE", 0.0)  # no room for a verbatim carry
    config = Config()
    config.num_ctx = 8_192
    config.compact_keep_unacted = True
    out = compact(_history(), _Summ(), config)
    text = "\n".join(m.get("content") or "" for m in out)
    assert "[File index:" in text and "- b.py: lines 1-39 read" in text
    assert "line b.py 1\n" not in text


def test_switch_off_keeps_the_old_policy():
    config = Config()
    config.num_ctx = 8_192
    config.compact_keep_unacted = False
    summ = _Summ()
    out = compact(_history(), summ, config)
    text = "\n".join(m.get("content") or "" for m in out)
    assert "Kept through compaction" not in text and "[File index:" not in text
    assert "line b.py" in summ.seen[0]  # b.py went to the prose summariser


class _FailingSumm:
    def chat(self, *a, **kw):
        raise RuntimeError("summariser down")


def test_summariser_failure_keeps_the_tail_and_the_newest_round():
    config = Config()
    config.num_ctx = 8_192
    config.compact_keep_unacted = True
    history = _history() + _read_round([f"r{k:02d}.py" for k in range(14)])  # 15 messages > 12
    out = compact(history, _FailingSumm(), config)
    assert out[0]["content"] == "sys"
    newest = len(history) - 15
    assert out[1:] == history[newest - (len(out) - 1 - 15):] and len(out) - 1 >= 15
    assert out[1]["role"] == "assistant" or out[1] is not history[-12]
    text = "\n".join(m.get("content") or "" for m in out)
    assert "line r00.py 39" in text and "line r13.py 39" in text
    assert sum(1 for m in out if m.get("role") == "tool") >= 14  # the whole round, no orphan cut
