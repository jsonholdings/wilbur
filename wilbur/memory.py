"""Per-project auto memory (2.74.0).

Owner 2026-11-27: "each time I ask him to work on the same project each new
session he starts from scratch". This keeps learned project knowledge outside
the repo, so it is never committed or mirrored:

    ~/.local/share/wilbur/memory/<name>-<hash>/   (0700; $WILBUR_MEMORY_DIR overrides the root)
        MEMORY.md              one-line index, regenerated from the fact files
        <fact>.md              one fact (frontmatter: name, type, description)
        sessions/<date>-<id>.md  automatic end-of-session summaries (newest N kept)

At session start the index plus the newest summary go into the system prompt
once, as part of the static prefix: they are never re-rendered mid-session, so
prompt checkpoints (2.40.1) survive a memory_save. Everything written passes
through redact.py first.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from . import redact as redact_mod
from .projectdir import git_root

TYPES = ("user", "feedback", "project", "reference")
LOAD_MAX_CHARS = 4000
FACT_MAX_CHARS = 4000
SESSIONS_KEPT = 10
RECALL_LABEL = ("Recalled project memory (from earlier sessions; it may be stale -- "
                "verify against the repo before acting on it):")


def memory_root() -> Path:
    return Path(os.environ.get("WILBUR_MEMORY_DIR",
                               Path.home() / ".local" / "share" / "wilbur" / "memory"))


def project_root(cwd: str | os.PathLike) -> Path | None:
    """The git root, else the dir itself; $HOME is never a project."""
    try:
        here = Path(cwd).expanduser().resolve()
    except OSError:
        return None
    root = git_root(here) or here
    if root == Path.home().resolve() or root == Path("/"):
        return None
    return root


def project_key(root: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip("-") or "project"
    return f"{name}-{hashlib.sha256(str(root).encode()).hexdigest()[:12]}"


def project_dir(cwd: str | os.PathLike, create: bool = False) -> Path | None:
    root = project_root(cwd)
    if root is None:
        return None
    path = memory_root() / project_key(root)
    if create:
        path.mkdir(parents=True, exist_ok=True)
        (path / "sessions").mkdir(exist_ok=True)
        for p in (memory_root(), path, path / "sessions"):
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass
    return path


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:60]


def _redact(text: str) -> str:
    return redact_mod.redact_text(text, redact_mod.secret_values())


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _parse(text: str) -> tuple[dict[str, str], str]:
    meta: dict[str, str] = {}
    if text.startswith("---\n"):
        head, _, body = text[4:].partition("\n---\n")
        for line in head.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip()] = value.strip()
        return meta, body.lstrip("\n")
    return meta, text


def _fact_files(pdir: Path) -> list[Path]:
    return sorted(p for p in pdir.glob("*.md") if p.name != "MEMORY.md")


def list_facts(cwd: str | os.PathLike) -> list[dict[str, str]]:
    pdir = project_dir(cwd)
    if pdir is None or not pdir.is_dir():
        return []
    facts = []
    for path in _fact_files(pdir):
        meta, body = _parse(path.read_text(errors="replace"))
        facts.append({"name": path.stem, "type": meta.get("type", "project"),
                      "description": meta.get("description", ""), "body": body})
    return facts


def _write_index(pdir: Path) -> None:
    lines = ["# Project memory", ""]
    for path in _fact_files(pdir):
        meta, _ = _parse(path.read_text(errors="replace"))
        lines.append(f"- {path.stem} ({meta.get('type', 'project')}): {meta.get('description', '')}")
    _atomic_write(pdir / "MEMORY.md", "\n".join(lines) + "\n")


def save_fact(cwd: str | os.PathLike, name: str, body: str, *, type: str = "project",
              description: str = "") -> tuple[str, str]:
    """Create or update one fact. Returns (status, name); status is
    created | updated | duplicate (same body already stored under another name)
    | error."""
    key = slug(name)
    if not key:
        return "error", "a memory needs a name"
    if type not in TYPES:
        return "error", f"type must be one of {', '.join(TYPES)}"
    pdir = project_dir(cwd, create=True)
    if pdir is None:
        return "error", "no project here ($HOME is not a project)"
    body = _redact(str(body).strip())[:FACT_MAX_CHARS]
    description = _redact(" ".join(str(description or body).split()))[:150]
    for other in list_facts(cwd):
        if other["name"] != key and other["body"].strip() == body:
            return "duplicate", other["name"]
    target = pdir / f"{key}.md"
    status = "updated" if target.exists() else "created"
    _atomic_write(target, f"---\nname: {key}\ntype: {type}\ndescription: {description}\n---\n\n{body}\n")
    _write_index(pdir)
    return status, key


def read_fact(cwd: str | os.PathLike, name: str) -> str | None:
    pdir = project_dir(cwd)
    if pdir is None:
        return None
    path = pdir / f"{slug(name)}.md"
    return path.read_text(errors="replace") if path.is_file() else None


def forget(cwd: str | os.PathLike, name: str) -> bool:
    """Delete one fact by its exact name."""
    pdir = project_dir(cwd)
    if pdir is None or not name or slug(name) != name:
        return False
    path = pdir / f"{name}.md"
    if not path.is_file():
        return False
    path.unlink()
    _write_index(pdir)
    return True


# -- session summaries ---------------------------------------------------- #

def _sessions(pdir: Path) -> list[Path]:
    """Summaries newest first, by the epoch recorded in their frontmatter."""
    found = []
    for path in (pdir / "sessions").glob("*.md"):
        meta, _ = _parse(path.read_text(errors="replace"))
        try:
            found.append((float(meta.get("ended", "0")), path))
        except ValueError:
            found.append((0.0, path))
    return [p for _, p in sorted(found, reverse=True)]


def newest_session(cwd: str | os.PathLike) -> tuple[dict[str, str], str] | None:
    pdir = project_dir(cwd)
    if pdir is None or not (pdir / "sessions").is_dir():
        return None
    files = _sessions(pdir)
    return _parse(files[0].read_text(errors="replace")) if files else None


def _args(call: dict[str, Any]) -> dict[str, Any]:
    raw = (call.get("function") or {}).get("arguments") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def summarise(messages: list[dict[str, Any]], state: Any = None) -> dict[str, Any]:
    """A deterministic summary built from the transcript and run state (no
    model call, so it works at exit and costs nothing)."""
    users = [m.get("content") or "" for m in messages
             if m.get("role") == "user" and not str(m.get("content") or "").startswith("[harness]")]
    goal = (getattr(state, "objective", "") or (users[0] if users else "")).strip()
    files: list[str] = []
    commands: list[str] = []
    for m in messages:
        for call in m.get("tool_calls") or []:
            name = (call.get("function") or {}).get("name", "")
            args = _args(call)
            if name in ("write_file", "edit_file") and args.get("path"):
                if args["path"] not in files:
                    files.append(str(args["path"]))
            elif name in ("run_bash", "bash", "run_tests"):
                cmd = str(args.get("command") or args.get("cmd") or "")
                if "git commit" in cmd or "pytest" in cmd or name == "run_tests":
                    commands.append(" ".join(cmd.split())[:160] or name)
    plan = list(getattr(state, "plan", []) or [])
    open_items = [i.text for i in plan if not getattr(i, "done", False)]
    verified = list(getattr(state, "verifications", []) or [])
    return {"goal": " ".join(goal.split())[:300], "files": files[:30],
            "commands": commands[-10:], "verified": [str(v)[:160] for v in verified[-10:]],
            "open": open_items[:15], "last_request": " ".join((users[-1] if users else "").split())[:300]}


def write_session_summary(cwd: str | os.PathLike, session_id: str,
                          messages: list[dict[str, Any]], state: Any = None) -> Path | None:
    """Write (or rewrite) sessions/<date>-<id>.md. Nothing is written for a
    session with no user turn. Keeps the newest SESSIONS_KEPT."""
    summary = summarise(messages, state)
    if not summary["goal"] and not summary["last_request"]:
        return None
    pdir = project_dir(cwd, create=True)
    if pdir is None:
        return None
    sid = slug(session_id) or "session"
    existing = list((pdir / "sessions").glob(f"*-{sid}.md"))
    target = existing[0] if existing else pdir / "sessions" / f"{time.strftime('%Y-%m-%d')}-{sid}.md"
    now = time.time()

    def section(title: str, items: list[str]) -> str:
        return f"## {title}\n" + ("\n".join(f"- {i}" for i in items) if items else "- (none)") + "\n"

    open_items = summary["open"]
    next_step = open_items[0] if open_items else (summary["last_request"] or "(none recorded)")
    root = project_root(cwd)
    head = _git_head(root) if root is not None else ""
    text = (f"---\nsession: {session_id}\nended: {now:.0f}\nhead: {head}\n"
            f"date: {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}\n---\n\n"
            f"## Goal\n{summary['goal'] or '(none recorded)'}\n\n"
            + section("What changed", summary["files"]) + "\n"
            + section("Commands run (commits, tests)", summary["commands"]) + "\n"
            + section("Verified", summary["verified"]) + "\n"
            + section("Open items", open_items) + "\n"
            + f"## Next step\n{next_step}\n")
    _atomic_write(target, _redact(text))
    for old in _sessions(pdir)[SESSIONS_KEPT:]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


# -- loading ------------------------------------------------------------- #

def load_block(cwd: str | os.PathLike, max_chars: int = LOAD_MAX_CHARS) -> str:
    """The prompt block for a new session: index + newest summary, capped.
    Over the cap the summary is cut first, then the index; the model reads the
    rest on demand with memory_read. '' when the project has no memory."""
    pdir = project_dir(cwd)
    if pdir is None or not pdir.is_dir():
        return ""
    index = (pdir / "MEMORY.md").read_text(errors="replace").strip() if (pdir / "MEMORY.md").is_file() else ""
    newest = newest_session(cwd)
    if not index and newest is None:
        return ""
    parts = [RECALL_LABEL]
    if index:
        parts.append(index)
    if newest is not None:
        meta, body = newest
        parts.append(f"Last session ({meta.get('date', '?')}):\n{body.strip()}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        note = "\n[memory truncated at the size cap; use memory_read <name> for the rest]"
        text = text[:max(0, max_chars - len(note))].rstrip() + note
    return text


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None


def _git_head(root: Path) -> str:
    out = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    return out.stdout.strip() if out is not None and out.returncode == 0 else ""


def commits_since(root: Path, epoch: float, limit: int = 10, head: str = "",
                  recorded_empty: bool = False) -> list[str]:
    """Commits after the recorded HEAD (exact), else after the epoch."""
    if head and re.fullmatch(r"[0-9a-f]{7,64}", head):
        out = _git(root, "log", "--oneline", f"-{limit}", f"{head}..HEAD")
        if out is not None and out.returncode == 0:
            return [line for line in out.stdout.splitlines() if line.strip()]
    if _git_head(root) == "":
        return []
    if head == "" and recorded_empty:
        # the repo had no commits when the session ended: every commit is new
        out = _git(root, "log", "--oneline", f"-{limit}")
        return [ln for ln in out.stdout.splitlines() if ln.strip()] if out is not None and out.returncode == 0 else []
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", f"--since=@{int(epoch)}", "--oneline", f"-{limit}"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.stdout.splitlines() if line.strip()] if out.returncode == 0 else []


def start_brief(cwd: str | os.PathLike) -> str:
    """'Resuming <project>: last session <date>, open items: ...' plus the
    commits made since then, or '' for a project with no summary yet."""
    newest = newest_session(cwd)
    root = project_root(cwd)
    if newest is None or root is None:
        return ""
    meta, body = newest
    match = re.search(r"## Open items\n(.*?)(?:\n## |\Z)", body, re.S)
    items = [ln[2:] for ln in (match.group(1).splitlines() if match else [])
             if ln.startswith("- ") and ln != "- (none)"]
    lines = [f"Resuming {root.name}: last session {meta.get('date', '?')}, open items: "
             + ("; ".join(items[:5]) if items else "none")]
    try:
        commits = commits_since(root, float(meta.get("ended", "0")), head=meta.get("head", ""),
                                recorded_empty="head" in meta)
    except ValueError:
        commits = []
    if commits:
        lines.append(f"{len(commits)} commit(s) since then:")
        lines += [f"  {c}" for c in commits]
    return "\n".join(lines)
