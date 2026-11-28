"""Refuse destructive git forms typed through run_bash; flag default-branch pushes.

BACKLOG tool-git. The owner's explicit-targets rule (global CLAUDE.md §0c) bans
`git add -A` / `git add .` / `git commit -a` / `git reset --hard` / `git clean` /
`git stash` in a shared tree: other sessions' uncommitted work lives in the same
working tree and index (2026-09-18 a helper stashed two running agents' work;
2026-09-11 `git add X && git commit` shipped another session's staged half rename).
Approval modes cannot catch these under approve-all, so like killguard this runs
in every mode, before approval. Read-only forms (`git stash list`, `git clean -n`)
are allowed.

A push to a default branch is not refused here; `default_branch_push` names the
branch so run_bash can ask for it under its own approval key (`git_push_default`),
which an `auto_approve` entry for `run_bash` or `git_push` does not cover.
"""
from __future__ import annotations

import os
import shlex
import subprocess

from .killguard import _SEGMENT_SPLIT, _WRAPPERS

DEFAULT_BRANCHES = ("main", "master")

REFUSAL = (
    "Refused without running: `{cmd}` {what}. Other work in this tree is not yours "
    "to sweep up or throw away. Name exact paths instead: git_commit(paths, message) "
    "commits only those paths (`git commit -- <paths>`), and `git restore -- <path>` / "
    "`git rm --cached -- <path>` undo one named file."
)

# git options that take a separate value before the subcommand
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                             "--exec-path", "--config-env"})
_STASH_READ_ONLY = frozenset({"list", "show"})


def _git_invocations(command: str) -> list[tuple[list[str], list[str], str]]:
    """(global opts, subcommand + args, segment text) for each git call in `command`."""
    found = []
    for segment in _SEGMENT_SPLIT.split(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment, comments=True)
        except ValueError:
            tokens = segment.split()
        index = 0
        while index < len(tokens):
            word = tokens[index]
            if "=" in word and word.split("=", 1)[0].isidentifier():
                index += 1
                continue
            name = os.path.basename(word)
            if name == "git":
                break
            if name not in _WRAPPERS:
                index = len(tokens)
                break
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                index += 1
        if index >= len(tokens):
            continue
        rest = tokens[index + 1:]
        opts: list[str] = []
        while rest and rest[0].startswith("-"):
            opt = rest.pop(0)
            opts.append(opt)
            if opt in _GIT_VALUE_OPTS and rest:
                opts.append(rest.pop(0))
        if rest:
            found.append((opts, rest, segment))
    return found


def _short_flags(args: list[str]) -> set[str]:
    flags = set()
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            flags.update(arg[1:])
    return flags


def _destructive(sub: str, args: list[str]) -> str | None:
    longs = {a.split("=", 1)[0] for a in args if a.startswith("--")}
    shorts = _short_flags(args)
    if sub == "add":
        paths = [a for a in args if not a.startswith("-")]
        if "--all" in longs or "A" in shorts or "--no-ignore-removal" in longs:
            return "stages every change in the tree (add -A)"
        if any(p in (".", ":/", "*", ":(top)") for p in paths):
            return "stages every change under the directory (add .)"
        if ("u" in shorts or "--update" in longs) and not paths:
            return "stages every tracked change (add -u)"
    if sub == "commit" and ("--all" in longs or "a" in shorts):
        return "commits every tracked change (commit -a)"
    if sub == "reset" and "--hard" in longs:
        return "discards uncommitted work (reset --hard)"
    if sub == "clean" and not ({"n"} & shorts or "--dry-run" in longs):
        return "deletes untracked files (clean)"
    if sub == "stash":
        action = next((a for a in args if not a.startswith("-")), "push")
        if action not in _STASH_READ_ONLY:
            return f"moves or drops uncommitted work (stash {action})"
    if sub == "checkout" and "--" in args and args[args.index("--") + 1:] in (["."], [":/"]):
        return "discards every unstaged change (checkout -- .)"
    if sub == "restore" and any(p in (".", ":/") for p in args if not p.startswith("-")):
        return "discards every unstaged change (restore .)"
    return None


def git_refusal(command: str) -> str | None:
    """The refusal message when `command` runs a destructive whole-tree git form."""
    for _opts, rest, segment in _git_invocations(command):
        what = _destructive(rest[0], rest[1:])
        if what:
            return REFUSAL.format(cmd=segment[:120], what=what)
    return None


def current_branch(cwd: str) -> str | None:
    try:
        proc = subprocess.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=cwd,
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


def default_branches(cwd: str, remote: str = "origin") -> set[str]:
    """main, master and whatever `<remote>/HEAD` points at."""
    names = set(DEFAULT_BRANCHES)
    try:
        proc = subprocess.run(["git", "symbolic-ref", "--short", "-q",
                               f"refs/remotes/{remote}/HEAD"], cwd=cwd,
                              capture_output=True, text=True, timeout=10)
        head = proc.stdout.strip()
        if head:
            names.add(head.split("/", 1)[1] if "/" in head else head)
    except (OSError, subprocess.SubprocessError):
        pass
    return names


def _ref_name(ref: str) -> str:
    ref = ref.lstrip("+")
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def push_targets(args: list[str], cwd: str) -> tuple[str, list[str]]:
    """(remote, destination branch names) of a `git push` argument list."""
    positional = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-o", "--push-option", "--repo", "--receive-pack", "--exec"):
            index += 2
            continue
        if not arg.startswith("-"):
            positional.append(arg)
        index += 1
    remote = positional[0] if positional else "origin"
    refspecs = positional[1:]
    if not refspecs:
        branch = current_branch(cwd)
        return remote, [branch] if branch else []
    targets = []
    for spec in refspecs:
        dst = spec.split(":", 1)[1] if ":" in spec else spec
        if dst in ("HEAD", "@"):
            dst = current_branch(cwd) or dst
        targets.append(_ref_name(dst))
    return remote, targets


def default_branch_push(command: str, cwd: str) -> str | None:
    """'<remote> <branch>' when `command` pushes to a default branch, else None."""
    for opts, rest, _segment in _git_invocations(command):
        if rest[0] != "push":
            continue
        where = cwd
        if "-C" in opts:
            where = os.path.join(cwd, opts[opts.index("-C") + 1])
        args = rest[1:]
        if "--all" in args or "--mirror" in args:
            return "every branch (--all/--mirror)"
        remote, targets = push_targets(args, where)
        hits = [t for t in targets if t in default_branches(where, remote)]
        if hits:
            return f"{remote} {hits[0]}"
    return None
