"""Per-turn file checkpoints and `/rewind` (BACKLOG checkpoints-rewind).

Before a write tool (edit_file, write_file, apply_patch) touches a file, the
file's current bytes are copied into the checkpoint of the current user turn,
once per file per turn. A file that did not exist is recorded as absent, so a
rewind deletes what Wilbur created and restores what Wilbur deleted. Nothing
here depends on git: it works on uncommitted work and outside a repository.

Storage: <root>/<project-key>/<turn-id>/ with manifest.json and blobs/,
directories 0700 and files 0600. The root is ~/.local/share/wilbur/checkpoints
(env WILBUR_CHECKPOINTS). The whole project directory is size-capped; the
oldest turns are pruned first, never the turn in progress.

Not tracked: anything run_bash does. /rewind says so every time.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASH_NOT_TRACKED = ("run_bash side effects (files a shell command made, changed or "
                    "deleted) are NOT tracked and were not restored.")
_PROMPT_KEEP = 200


def checkpoints_root() -> Path:
    """Read at call time so a test (or a temp home) redirects it."""
    return Path(os.environ.get(
        "WILBUR_CHECKPOINTS",
        Path.home() / ".local" / "share" / "wilbur" / "checkpoints"))


def project_key(cwd: str) -> str:
    real = os.path.realpath(cwd)
    name = "".join(c if c.isalnum() or c in "-_." else "_" for c in os.path.basename(real)) or "root"
    return f"{name}-{hashlib.sha256(real.encode()).hexdigest()[:12]}"


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _dir_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


@dataclass
class Turn:
    turn_id: str
    created: float
    prompt: str
    message_index: int
    # absolute path -> blob name, or None when the file did not exist
    files: dict[str, str | None] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"turn_id": self.turn_id, "created": self.created, "prompt": self.prompt,
                "message_index": self.message_index, "files": self.files}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Turn":
        return cls(turn_id=str(data["turn_id"]), created=float(data.get("created", 0)),
                   prompt=str(data.get("prompt", "")),
                   message_index=int(data.get("message_index", 0)),
                   files=dict(data.get("files") or {}))


@dataclass
class RewindResult:
    turn: Turn
    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class CheckpointStore:
    """One store per (root, project). Agents in the same project (a REPL agent
    and the subagents it spawns) share it, so a subagent's writes land in the
    parent's current turn."""

    def __init__(self, root: Path, cwd: str, max_bytes: int) -> None:
        self.dir = Path(root) / project_key(cwd)
        self.max_bytes = max(0, int(max_bytes))
        self.current: Turn | None = None
        self._lock = threading.RLock()

    # -- recording ---------------------------------------------------------
    def _turn_dir(self, turn_id: str) -> Path:
        return self.dir / turn_id

    def _save(self, turn: Turn) -> None:
        _write_private(self._turn_dir(turn.turn_id) / "manifest.json",
                       json.dumps(turn.to_json(), indent=1).encode())

    def _next_id(self) -> str:
        highest = 0
        if self.dir.is_dir():
            for child in self.dir.iterdir():
                if child.name.isdigit():
                    highest = max(highest, int(child.name))
        return f"{highest + 1:06d}"

    def begin_turn(self, prompt: str, message_index: int) -> Turn:
        with self._lock:
            _mkdir_private(self.dir.parent)
            _mkdir_private(self.dir)
            turn = Turn(turn_id=self._next_id(), created=time.time(),
                        prompt=str(prompt)[:_PROMPT_KEEP], message_index=int(message_index))
            _mkdir_private(self._turn_dir(turn.turn_id))
            self._save(turn)
            self.current = turn
            self.prune()
            return turn

    def before_write(self, path: str | os.PathLike) -> bool:
        """Snapshot `path` (absolute) into the current turn, once per turn.
        True when this call recorded a new entry."""
        with self._lock:
            turn = self.current
            if turn is None:
                return False
            key = os.path.abspath(os.fspath(path))
            if key in turn.files:
                return False
            blob: str | None = None
            target = Path(key)
            if target.is_file():
                data = target.read_bytes()
                blob = hashlib.sha256(data).hexdigest()
                blobs = self._turn_dir(turn.turn_id) / "blobs"
                _mkdir_private(blobs)
                if not (blobs / blob).exists():
                    _write_private(blobs / blob, data)
            elif target.exists():
                return False  # a directory or special file: not ours to snapshot
            turn.files[key] = blob
            self._save(turn)
            self.prune()
            return True

    def forget_if_unchanged(self, path: str | os.PathLike) -> bool:
        """Drop the current turn's entry for `path` when the file is still
        exactly as snapshotted: the write was refused (denied, guarded, failed
        before writing), so the turn did not touch it and /rewind must not
        list or restore it. True when the entry was dropped."""
        with self._lock:
            turn = self.current
            if turn is None:
                return False
            key = os.path.abspath(os.fspath(path))
            if key not in turn.files:
                return False
            blob = turn.files[key]
            target = Path(key)
            if blob is None:
                unchanged = not target.exists()
            else:
                unchanged = (target.is_file()
                             and hashlib.sha256(target.read_bytes()).hexdigest() == blob)
            if not unchanged:
                return False
            del turn.files[key]
            if blob is not None and blob not in turn.files.values():
                orphan = self._turn_dir(turn.turn_id) / "blobs" / blob
                if orphan.is_file():
                    orphan.unlink()
            self._save(turn)
            return True

    # -- reading -----------------------------------------------------------
    def turns(self) -> list[Turn]:
        """Every stored turn, oldest first."""
        out: list[Turn] = []
        if not self.dir.is_dir():
            return out
        for child in sorted(self.dir.iterdir()):
            manifest = child / "manifest.json"
            if child.name.isdigit() and manifest.is_file():
                try:
                    out.append(Turn.from_json(json.loads(manifest.read_text())))
                except (OSError, ValueError, KeyError):
                    continue
        return out

    def prune(self) -> list[str]:
        """Delete the oldest turns until the project is under max_bytes."""
        removed: list[str] = []
        if self.max_bytes <= 0 or not self.dir.is_dir():
            return removed
        with self._lock:
            current = self.current.turn_id if self.current else None
            for turn in self.turns():
                if _dir_bytes(self.dir) <= self.max_bytes:
                    break
                if turn.turn_id == current:
                    continue
                shutil.rmtree(self._turn_dir(turn.turn_id), ignore_errors=True)
                removed.append(turn.turn_id)
        return removed

    # -- rewinding ---------------------------------------------------------
    def rewind_code(self, turn_id: str) -> RewindResult:
        """Put every file back as it was before `turn_id` began. Turns from
        `turn_id` on are applied newest first, so the earliest snapshot of a
        file wins; their checkpoints are then dropped."""
        with self._lock:
            later = [t for t in self.turns() if t.turn_id >= turn_id]
            if not later or later[0].turn_id != turn_id:
                raise KeyError(turn_id)
            result = RewindResult(turn=later[0])
            final: dict[str, tuple[Turn, str | None]] = {}
            for turn in reversed(later):
                for path, blob in turn.files.items():
                    final[path] = (turn, blob)
            for path, (turn, blob) in sorted(final.items()):
                target = Path(path)
                try:
                    if blob is None:
                        if target.is_file() or target.is_symlink():
                            target.unlink()
                            result.removed.append(path)
                    else:
                        data = (self._turn_dir(turn.turn_id) / "blobs" / blob).read_bytes()
                        target.parent.mkdir(parents=True, exist_ok=True)
                        mode = target.stat().st_mode & 0o7777 if target.exists() else None
                        tmp = target.with_name(target.name + ".wilbur-rewind.tmp")
                        tmp.write_bytes(data)
                        if mode is not None:
                            os.chmod(tmp, mode)
                        os.replace(tmp, target)
                        result.restored.append(path)
                except OSError as exc:
                    result.errors.append(f"{path}: {exc}")
            for turn in later:
                shutil.rmtree(self._turn_dir(turn.turn_id), ignore_errors=True)
            self.current = None
            return result

    def find(self, turn_id: str) -> Turn | None:
        return next((t for t in self.turns() if t.turn_id == turn_id), None)


def conversation_cut(messages: list[dict[str, Any]], turn: Turn) -> int | None:
    """Index to truncate `messages` at so the conversation is as it was before
    `turn`'s prompt was sent, or None when compaction has rewritten that part
    of the transcript and the prompt can no longer be found."""
    def matches(i: int) -> bool:
        m = messages[i]
        return (m.get("role") == "user"
                and str(m.get("content", ""))[:_PROMPT_KEEP] == turn.prompt)
    i = turn.message_index
    if 0 < i < len(messages) and matches(i):
        return i
    for j in range(len(messages) - 1, 0, -1):
        if matches(j):
            return j
    return None


_STORES: dict[tuple[str, str], CheckpointStore] = {}
_STORES_LOCK = threading.Lock()


def store_for(config: Any, cwd: str) -> CheckpointStore | None:
    """The shared store for this project, or None when checkpoints are off."""
    if not getattr(config, "checkpoints", True):
        return None
    root = checkpoints_root()
    key = (str(root), project_key(cwd))
    max_bytes = int(getattr(config, "checkpoints_max_mb", 200)) * 1024 * 1024
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = _STORES[key] = CheckpointStore(root, cwd, max_bytes)
        store.max_bytes = max_bytes
        return store


REWIND_USAGE = ("/rewind lists recent turns; /rewind <n> [both|code|chat] restores turn n "
                "(1 = newest) -- code = files only, chat = conversation only, both = default.")


def rewind_command(agent: Any, arg: str, limit: int = 10) -> list[str]:
    """The /rewind command, kept out of the REPL so it is testable alone.
    Returns the lines to print."""
    store = store_for(agent.config, str(agent.ctx.cwd))
    if store is None:
        return ["checkpoints are off (config checkpoints=false): nothing to rewind."]
    turns = list(reversed(store.turns()))  # newest first
    parts = arg.split()
    if not parts:
        if not turns:
            return ["no checkpoints for this project yet.", BASH_NOT_TRACKED]
        lines = ["recent turns (newest first):"]
        for n, turn in enumerate(turns[:limit], 1):
            when = time.strftime("%H:%M", time.localtime(turn.created))
            prompt = " ".join(turn.prompt.split())[:60]
            lines.append(f"  {n:>2}  {when}  {len(turn.files)} file(s)  {prompt}")
        lines += [REWIND_USAGE, BASH_NOT_TRACKED]
        return lines
    mode = parts[1].lower() if len(parts) > 1 else "both"
    if not parts[0].isdigit() or mode not in ("both", "code", "chat") or len(parts) > 2:
        return [REWIND_USAGE]
    n = int(parts[0])
    if not 1 <= n <= len(turns):
        return [f"no turn {n}: {len(turns)} stored. " + REWIND_USAGE]
    turn = turns[n - 1]
    lines: list[str] = []
    if mode in ("both", "chat"):
        cut = conversation_cut(agent.messages, turn)
        if cut is None:
            lines.append("conversation not rewound: that prompt was compacted away.")
        else:
            agent.messages = agent.messages[:cut]
            lines.append(f"conversation rewound to before: {turn.prompt[:60]!r}")
    if mode in ("both", "code"):
        result = store.rewind_code(turn.turn_id)
        lines.append(f"files rewound: {len(result.restored)} restored, "
                     f"{len(result.removed)} removed (created by Wilbur).")
        lines += [f"  error: {e}" for e in result.errors]
    lines.append(BASH_NOT_TRACKED)
    return lines
