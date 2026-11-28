"""Git tools that return structured data and commit only named paths.

BACKLOG tool-git. `git_status`, `git_diff`, `git_log` and `git_blame` return
JSON the model can act on without scraping porcelain text. `git_commit(paths,
message)` runs `git commit --only -- <paths>`: other files already staged in
the shared index stay staged and are not committed (2026-09-11: `git add X &&
git commit` shipped another session's staged half rename live). Whole-tree
paths (`.`, `:/`, the repo root) are refused. `git_push` asks under its own
approval key `git_push_default` when the target is a default branch, so an
`auto_approve` entry for `git_push` never covers main/master.
`git_tools: false` removes all five from the registry.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .base import Approval, ToolContext, ToolResult, clamp, denial_message, outside_cwd
from . import gitguard

_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat", "LC_ALL": "C"}
_WHOLE_TREE = frozenset({".", "./", ":/", ":(top)", "*", "-A", "--all"})


def _git(ctx: ToolContext, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ, **_ENV)
    return subprocess.run(["git", "--no-pager", *args], cwd=str(ctx.cwd), env=env,
                          capture_output=True, text=True, timeout=timeout)


def _fail(proc: subprocess.CompletedProcess, what: str) -> ToolResult:
    err = (proc.stderr or proc.stdout or "").strip()
    return ToolResult(f"{what} failed (exit {proc.returncode}): {err[:2000]}", is_error=True)


def _json(ctx: ToolContext, data: Any) -> ToolResult:
    text = json.dumps(data, indent=1)
    return ToolResult(clamp(text, ctx.config.max_tool_output_chars, ctx.config.max_tool_output_lines))


def _paths_arg(args: dict[str, Any]) -> list[str]:
    paths = args.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    return [str(p) for p in paths if str(p).strip()]


def _bad_path(path: str, ctx: ToolContext) -> str | None:
    if path.startswith("-"):
        return f"{path!r} looks like an option, not a path"
    if path.strip() in _WHOLE_TREE or any(ch in path for ch in "*?["):
        return f"{path!r} names many files; list each file"
    target = (Path(ctx.cwd) / path)
    if outside_cwd(target, ctx.cwd):
        return f"{path!r} is outside the working directory"
    return None


# ------------------------------------------------------------------ status

def parse_status(text: str) -> dict[str, Any]:
    """Parse `git status --porcelain=v2 --branch -z`."""
    out: dict[str, Any] = {"branch": None, "upstream": None, "ahead": 0, "behind": 0,
                           "staged": [], "unstaged": [], "untracked": [], "conflicted": []}
    records = text.split("\0")
    index = 0
    while index < len(records):
        rec = records[index]
        index += 1
        if not rec:
            continue
        if rec.startswith("# branch.head "):
            head = rec[len("# branch.head "):]
            out["branch"] = None if head == "(detached)" else head
        elif rec.startswith("# branch.upstream "):
            out["upstream"] = rec[len("# branch.upstream "):]
        elif rec.startswith("# branch.ab "):
            ahead, behind = rec[len("# branch.ab "):].split()
            out["ahead"], out["behind"] = int(ahead), -int(behind)
        elif rec.startswith("? "):
            out["untracked"].append(rec[2:])
        elif rec.startswith("u "):
            out["conflicted"].append(rec.split(" ", 10)[10])
        elif rec.startswith(("1 ", "2 ")):
            kind = rec[0]
            fields = rec.split(" ", 8 if kind == "1" else 9)
            xy, path = fields[1], fields[-1]
            entry = {"path": path}
            if kind == "2":
                entry["from"] = records[index]
                index += 1
            if xy[0] != ".":
                out["staged"].append({**entry, "change": _CODES.get(xy[0], xy[0])})
            if xy[1] != ".":
                out["unstaged"].append({**entry, "change": _CODES.get(xy[1], xy[1])})
    out["clean"] = not any(out[k] for k in ("staged", "unstaged", "untracked", "conflicted"))
    return out


_CODES = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied",
          "T": "type-changed"}


class GitStatus:
    name = "git_status"
    read_only = True
    description = ("Git status as JSON: branch, upstream, ahead/behind, staged, unstaged, "
                   "untracked and conflicted files.")
    schema = {"type": "object", "properties": {}}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        proc = _git(ctx, ["status", "--porcelain=v2", "--branch", "-z"])
        if proc.returncode:
            return _fail(proc, "git status")
        return _json(ctx, parse_status(proc.stdout))


# -------------------------------------------------------------------- diff

def parse_diff(text: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    hunk: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = {"path": None, "from": None, "status": "modified", "added": 0,
                       "deleted": 0, "binary": False, "hunks": []}
            files.append(current)
            hunk = None
            continue
        if current is None:
            continue
        if hunk is None:
            if line.startswith("new file mode"):
                current["status"] = "added"
            elif line.startswith("deleted file mode"):
                current["status"] = "deleted"
            elif line.startswith("rename from "):
                current["status"], current["from"] = "renamed", line[len("rename from "):]
            elif line.startswith("rename to "):
                current["path"] = line[len("rename to "):]
            elif line.startswith("Binary files "):
                current["binary"] = True
            elif line.startswith("--- "):
                src = line[4:]
                if src != "/dev/null":
                    current["from"] = current["from"] or src[2:]
            elif line.startswith("+++ "):
                dst = line[4:]
                current["path"] = current["from"] if dst == "/dev/null" else dst[2:]
        if line.startswith("@@"):
            head = line.split("@@")[1].split()
            old, new = head[0][1:].split(","), head[1][1:].split(",")
            hunk = {"old_start": int(old[0]), "old_lines": int(old[1]) if len(old) > 1 else 1,
                    "new_start": int(new[0]), "new_lines": int(new[1]) if len(new) > 1 else 1,
                    "section": line.split("@@", 2)[2].strip(), "lines": []}
            current["hunks"].append(hunk)
            continue
        if hunk is not None and line[:1] in ("+", "-", " ", "\\"):
            hunk["lines"].append(line)
            if line.startswith("+"):
                current["added"] += 1
            elif line.startswith("-"):
                current["deleted"] += 1
    for f in files:
        if f["path"] is None:
            f["path"] = f["from"]
        if f["from"] == f["path"]:
            f["from"] = None
    return files


class GitDiff:
    name = "git_diff"
    read_only = True
    description = ("Git diff as JSON per file (status, added/deleted counts, hunks). "
                   "staged=true for the index; ref compares against a commit; paths limits it.")
    schema = {"type": "object", "properties": {
        "staged": {"type": "boolean"},
        "ref": {"type": "string", "description": "commit or range, e.g. HEAD~1 or main..HEAD"},
        "paths": {"type": "array", "items": {"type": "string"}},
    }}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        cmd = ["diff", "--no-color", "--no-ext-diff", "-M"]
        if args.get("staged"):
            cmd.append("--cached")
        ref = str(args.get("ref") or "").strip()
        if ref:
            if ref.startswith("-"):
                return ToolResult(f"ref {ref!r} looks like an option.", is_error=True)
            cmd.append(ref)
        cmd += ["--", *_paths_arg(args)]
        proc = _git(ctx, cmd)
        if proc.returncode:
            return _fail(proc, "git diff")
        return _json(ctx, {"files": parse_diff(proc.stdout)})


# --------------------------------------------------------------------- log

_SEP, _END = "\x1f", "\x1e"


def parse_log(text: str) -> list[dict[str, str]]:
    commits = []
    for rec in text.split(_END):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, author, email, date, subject = rec.split(_SEP, 4)
        commits.append({"sha": sha, "author": author, "email": email, "date": date,
                        "subject": subject})
    return commits


class GitLog:
    name = "git_log"
    read_only = True
    description = "Git log as JSON (sha, author, date, subject); optional ref, paths, limit."
    schema = {"type": "object", "properties": {
        "ref": {"type": "string"},
        "paths": {"type": "array", "items": {"type": "string"}},
        "limit": {"type": "integer", "description": "default 20, max 200"},
    }}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        limit = max(1, min(int(args.get("limit") or 20), 200))
        fmt = _SEP.join(["%H", "%an", "%ae", "%aI", "%s"]) + _END
        cmd = ["log", f"-n{limit}", f"--format={fmt}"]
        ref = str(args.get("ref") or "").strip()
        if ref:
            if ref.startswith("-"):
                return ToolResult(f"ref {ref!r} looks like an option.", is_error=True)
            cmd.append(ref)
        cmd += ["--", *_paths_arg(args)]
        proc = _git(ctx, cmd)
        if proc.returncode:
            return _fail(proc, "git log")
        return _json(ctx, {"commits": parse_log(proc.stdout)})


# ------------------------------------------------------------------- blame

def parse_blame(text: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    meta: dict[str, dict[str, str]] = {}
    current: dict[str, Any] | None = None
    for raw in text.split("\n"):
        if current is None:
            parts = raw.split()
            if len(parts) >= 3 and len(parts[0]) >= 40:
                sha = parts[0]
                current = {"line": int(parts[2]), "sha": sha}
                meta.setdefault(sha, {})
            continue
        if raw.startswith("\t"):
            info = meta[current["sha"]]
            current.update(author=info.get("author", ""), date=info.get("author-time", ""),
                           summary=info.get("summary", ""), text=raw[1:])
            lines.append(current)
            current = None
            continue
        key, _, value = raw.partition(" ")
        meta[current["sha"]][key] = value
    return lines


class GitBlame:
    name = "git_blame"
    read_only = True
    description = "Git blame as JSON per line (line, sha, author, date, summary, text)."
    schema = {"type": "object", "properties": {
        "path": {"type": "string"},
        "start": {"type": "integer"},
        "end": {"type": "integer"},
    }, "required": ["path"]}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = str(args.get("path") or "")
        bad = _bad_path(path, ctx) if path else "no path given"
        if bad:
            return ToolResult(f"git_blame: {bad}.", is_error=True)
        cmd = ["blame", "--line-porcelain"]
        start, end = args.get("start"), args.get("end")
        if start:
            cmd.append(f"-L{int(start)},{int(end) if end else ''}")
        proc = _git(ctx, [*cmd, "--", path])
        if proc.returncode:
            return _fail(proc, "git blame")
        return _json(ctx, {"path": path, "lines": parse_blame(proc.stdout)})


# ------------------------------------------------------------------ commit

class GitCommit:
    name = "git_commit"
    read_only = False
    description = ("Commit ONLY the named paths (git commit -- <paths>); other staged files stay "
                   "staged. New files are added first. No '.', globs or -A.")
    schema = {"type": "object", "properties": {
        "paths": {"type": "array", "items": {"type": "string"}},
        "message": {"type": "string"},
    }, "required": ["paths", "message"]}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        paths = _paths_arg(args)
        message = str(args.get("message") or "").strip()
        if not paths:
            return ToolResult("git_commit needs at least one path; it never commits the "
                              "whole tree.", is_error=True)
        if not message:
            return ToolResult("git_commit needs a message.", is_error=True)
        for path in paths:
            bad = _bad_path(path, ctx)
            if bad:
                return ToolResult(f"Refused: {bad}.", is_error=True)
            if Path(ctx.cwd, path).resolve() == Path(ctx.cwd).resolve():
                return ToolResult(f"Refused: {path!r} is the whole working directory.",
                                  is_error=True)
        top = _git(ctx, ["rev-parse", "--show-toplevel"])
        if top.returncode:
            return _fail(top, "git rev-parse")
        if Path(top.stdout.strip()).resolve() in {Path(ctx.cwd, p).resolve() for p in paths}:
            return ToolResult("Refused: a path names the repository root.", is_error=True)
        outcome = ctx.approve("git_commit", f"commit {len(paths)} path(s): "
                              f"{', '.join(paths)}\n    message: {message.splitlines()[0][:100]}")
        if outcome is not Approval.GRANTED:
            return ToolResult(denial_message("commit", outcome), is_error=True)
        untracked = _git(ctx, ["ls-files", "--others", "--exclude-standard", "-z", "--", *paths])
        new_files = [p for p in untracked.stdout.split("\0") if p]
        if new_files:
            added = _git(ctx, ["add", "--", *new_files])
            if added.returncode:
                return _fail(added, "git add")
        proc = _git(ctx, ["commit", "--only", "-m", message, "--", *paths], timeout=300)
        if proc.returncode:
            if new_files:  # leave the index as it was
                _git(ctx, ["rm", "--cached", "-q", "--", *new_files])
            return _fail(proc, "git commit")
        sha = _git(ctx, ["rev-parse", "HEAD"]).stdout.strip()
        names = _git(ctx, ["show", "--name-status", "--format=", "HEAD"]).stdout
        files = [{"change": ln.split("\t")[0], "path": ln.split("\t")[-1]}
                 for ln in names.splitlines() if "\t" in ln]
        return _json(ctx, {"sha": sha, "files": files})


# -------------------------------------------------------------------- push

class GitPush:
    name = "git_push"
    read_only = False
    description = ("Push the current branch (or the named branch) to remote (default origin). A push to "
                   "main/master/the remote's default branch needs the user's approval. No force.")
    schema = {"type": "object", "properties": {
        "remote": {"type": "string"},
        "branch": {"type": "string"},
    }}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        remote = str(args.get("remote") or "origin").strip()
        branch = str(args.get("branch") or "").strip() or gitguard.current_branch(str(ctx.cwd))
        if not branch:
            return ToolResult("git_push: no current branch (detached HEAD); name one.",
                              is_error=True)
        if remote.startswith("-") or branch.startswith(("-", "+")) or ":" in branch:
            return ToolResult("git_push: plain remote and branch names only (no options, "
                              "force or refspecs).", is_error=True)
        if branch in gitguard.default_branches(str(ctx.cwd), remote):
            key, detail = "git_push_default", f"push to DEFAULT branch {remote}/{branch}"
        else:
            key, detail = "git_push", f"push {branch} to {remote}"
        outcome = ctx.approve(key, detail)
        if outcome is not Approval.GRANTED:
            return ToolResult(denial_message("push", outcome), is_error=True)
        proc = _git(ctx, ["push", remote, f"{branch}:{branch}"], timeout=300)
        if proc.returncode:
            return _fail(proc, "git push")
        return _json(ctx, {"remote": remote, "branch": branch,
                           "output": (proc.stderr + proc.stdout).strip()[-1500:]})


def git_tools() -> list[Any]:
    return [GitStatus(), GitDiff(), GitLog(), GitBlame(), GitCommit(), GitPush()]
