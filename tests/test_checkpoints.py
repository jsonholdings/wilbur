"""checkpoints-rewind: per-turn file snapshots and /rewind (wilbur.checkpoints).

Storage goes to the per-run temp home conftest points WILBUR_CHECKPOINTS at,
or to a tmp_path set here; never to the real ~/.local/share/wilbur."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from wilbur import checkpoints as cp
from wilbur.llm import Reply, ToolCall


@pytest.fixture(autouse=True)
def _private_root(monkeypatch, tmp_path_factory):
    root = tmp_path_factory.mktemp("ckpt-root")
    monkeypatch.setenv("WILBUR_CHECKPOINTS", str(root))
    return root


def _calls(*calls: ToolCall) -> Reply:
    return Reply(content="", tool_calls=list(calls))


def _call(name: str, **args) -> ToolCall:
    return ToolCall(name=name, arguments=args)


def _done(text: str = "done") -> Reply:
    return Reply(content=text, tool_calls=[])


def _turn(agent, prompt: str) -> str:
    agent.begin_user_turn()
    return agent.run(prompt)


BINARY = bytes(range(256)) + b"\x00\xff\r\n\x00tail"


def test_edit_write_patch_then_rewind_restores_bytes_exactly(make_agent, tmp_path):
    (tmp_path / "a.txt").write_bytes(b"alpha\r\nline two\n")
    (tmp_path / "b.txt").write_bytes(b"beta\n")
    (tmp_path / "c.txt").write_bytes(b"gamma\n")
    before = {n: (tmp_path / n).read_bytes() for n in ("a.txt", "b.txt", "c.txt")}
    agent, _ = make_agent([
        _calls(_call("read_file", path="a.txt"), _call("read_file", path="c.txt")),
        _calls(_call("read_file", path="b.txt")),
        _calls(_call("edit_file", path="a.txt", old_string="alpha", new_string="ALPHA")),
        _calls(_call("write_file", path="b.txt", content="now text\n")),
        _calls(_call("apply_patch", edits=[{"path": "c.txt", "old_string": "gamma",
                                            "new_string": "GAMMA"}])),
        _done(),
    ])
    _turn(agent, "change three files")
    assert (tmp_path / "a.txt").read_bytes() != before["a.txt"]
    assert (tmp_path / "b.txt").read_bytes() == b"now text\n"
    assert (tmp_path / "c.txt").read_bytes() == b"GAMMA\n"

    lines = cp.rewind_command(agent, "1 code")
    assert any("3 restored" in line for line in lines)
    assert cp.BASH_NOT_TRACKED in lines
    for name, data in before.items():
        assert (tmp_path / name).read_bytes() == data, name


def test_binary_snapshot_round_trips_exactly(tmp_path, _private_root):
    # Wilbur's own tools refuse to overwrite a binary file, but a patch can
    # delete one and a file can turn binary mid-turn; the store is byte-exact.
    store = cp.CheckpointStore(_private_root, str(tmp_path), max_bytes=10**6)
    target = tmp_path / "blob.bin"
    target.write_bytes(BINARY)
    os.chmod(target, 0o751)
    turn = store.begin_turn("binary", 1)
    store.before_write(target)
    target.write_bytes(b"clobbered")
    result = store.rewind_code(turn.turn_id)
    assert result.restored == [str(target)] and not result.errors
    assert target.read_bytes() == BINARY
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o751


def test_created_file_removed_and_deleted_file_restored(make_agent, tmp_path):
    (tmp_path / "old.txt").write_bytes(b"keep me\n")
    deletion = ("--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-keep me\n")
    agent, _ = make_agent([
        _calls(_call("read_file", path="old.txt")),
        _calls(_call("write_file", path="new/made.txt", content="fresh\n")),
        _calls(_call("apply_patch", patch=deletion)),
        _done(),
    ])
    _turn(agent, "create one, delete one")
    assert (tmp_path / "new" / "made.txt").exists()
    assert not (tmp_path / "old.txt").exists()

    lines = cp.rewind_command(agent, "1")
    assert any("1 restored, 1 removed" in line for line in lines), lines
    assert not (tmp_path / "new" / "made.txt").exists()
    assert (tmp_path / "old.txt").read_bytes() == b"keep me\n"


def _two_turns(make_agent, tmp_path):
    (tmp_path / "f.txt").write_text("v0\n")
    agent, _ = make_agent([
        _calls(_call("read_file", path="f.txt")),
        _calls(_call("edit_file", path="f.txt", old_string="v0", new_string="v1")),
        _done("first done"),
        _calls(_call("read_file", path="f.txt")),
        _calls(_call("edit_file", path="f.txt", old_string="v1", new_string="v2")),
        _done("second done"),
    ])
    _turn(agent, "first prompt")
    _turn(agent, "second prompt")
    assert (tmp_path / "f.txt").read_text() == "v2\n"
    return agent


def _user_prompts(agent) -> list[str]:
    return [m["content"] for m in agent.messages if m.get("role") == "user"]


def test_rewind_conversation_only_keeps_files(make_agent, tmp_path):
    agent = _two_turns(make_agent, tmp_path)
    listing = cp.rewind_command(agent, "")
    assert "second prompt" in listing[1] and "first prompt" in listing[2]
    cp.rewind_command(agent, "1 chat")
    assert "second prompt" not in _user_prompts(agent)
    assert "first prompt" in _user_prompts(agent)
    assert (tmp_path / "f.txt").read_text() == "v2\n"


def test_rewind_code_only_keeps_conversation(make_agent, tmp_path):
    agent = _two_turns(make_agent, tmp_path)
    cp.rewind_command(agent, "1 code")
    assert (tmp_path / "f.txt").read_text() == "v1\n"
    assert "second prompt" in _user_prompts(agent)


def test_rewind_to_an_older_turn_undoes_every_later_turn(make_agent, tmp_path):
    agent = _two_turns(make_agent, tmp_path)
    cp.rewind_command(agent, "2")
    assert (tmp_path / "f.txt").read_text() == "v0\n"
    assert "first prompt" not in _user_prompts(agent)


def test_cap_prunes_oldest_turns(tmp_path, _private_root):
    store = cp.CheckpointStore(_private_root, str(tmp_path), max_bytes=6000)
    target = tmp_path / "big.bin"
    for n in range(5):
        target.write_bytes(os.urandom(2500))
        store.begin_turn(f"turn {n}", 1)
        store.before_write(target)
    kept = [t.prompt for t in store.turns()]
    assert "turn 4" in kept and "turn 0" not in kept
    assert len(kept) < 5


def test_storage_is_private(tmp_path, _private_root):
    store = cp.CheckpointStore(_private_root, str(tmp_path), max_bytes=10**6)
    (tmp_path / "x").write_bytes(b"x")
    turn = store.begin_turn("p", 1)
    store.before_write(tmp_path / "x")
    assert stat.S_IMODE(os.stat(store.dir).st_mode) == 0o700
    blobs = store.dir / turn.turn_id / "blobs"
    assert stat.S_IMODE(os.stat(blobs).st_mode) == 0o700
    for f in blobs.iterdir():
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_non_git_directory_works(make_agent, tmp_path):
    assert not any((p / ".git").exists() for p in [tmp_path, *tmp_path.parents])
    (tmp_path / "plain.txt").write_text("plain\n")
    agent, _ = make_agent([
        _calls(_call("read_file", path="plain.txt")),
        _calls(_call("write_file", path="plain.txt", content="changed\n")),
        _done(),
    ])
    _turn(agent, "outside git")
    cp.rewind_command(agent, "1")
    assert (tmp_path / "plain.txt").read_text() == "plain\n"


def test_switch_off_takes_no_snapshots(make_agent, tmp_path, _private_root):
    # Control: the same scripted turn with checkpoints on does store a turn.
    (tmp_path / "s.txt").write_text("s\n")
    script = [
        _calls(_call("read_file", path="s.txt")),
        _calls(_call("write_file", path="s.txt", content="t\n")),
        _done(),
    ]
    on, _ = make_agent(list(script))
    _turn(on, "on")
    store = cp.store_for(on.config, str(tmp_path))
    assert [len(t.files) for t in store.turns()] == [1]

    (tmp_path / "s.txt").write_text("s\n")
    for turn in store.turns():
        store.rewind_code(turn.turn_id)
    off, _ = make_agent(list(script), checkpoints=False)
    _turn(off, "off")
    assert store.turns() == []
    assert cp.rewind_command(off, "")[0].startswith("checkpoints are off")
    assert not any(Path(_private_root).rglob("manifest.json"))


def test_refused_write_takes_no_snapshot(make_agent, tmp_path):
    # write_file on an existing file not read first is refused inside tool.run
    # (after the pre-write snapshot); the snapshot must be dropped again.
    (tmp_path / "r.txt").write_text("keep\n")
    (tmp_path / "w.txt").write_text("w\n")
    agent, _ = make_agent([
        _calls(_call("write_file", path="r.txt", content="clobber\n")),
        _calls(_call("read_file", path="w.txt")),
        _calls(_call("write_file", path="w.txt", content="x\n")),
        _done(),
    ])
    _turn(agent, "one refused, one real write")
    assert (tmp_path / "r.txt").read_text() == "keep\n"
    store = cp.store_for(agent.config, str(tmp_path))
    [turn] = store.turns()
    # Control: the real write is still snapshotted.
    assert sorted(Path(p).name for p in turn.files) == ["w.txt"]
