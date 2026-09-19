"""Isolated git worktrees for agent-driven coding.

A coding agent must never touch the files the user currently has checked
out and possibly has open in an editor, mid-edit, or mid-review. Running
the agent's changes in a separate `git worktree` on its own branch gives
it a real, fully-functional checkout that is physically a different
directory from the user's working tree, while still sharing the same
object database and history. The agent can build, test, and commit
freely; nothing it does is visible in the user's working tree until (and
unless) the result is explicitly merged back with `merge()`.

This module is intentionally dependency-free (stdlib `subprocess` only)
so it can be reused anywhere in wilbur without pulling in git bindings.
"""

from __future__ import annotations

import os
import subprocess


class WorktreeError(RuntimeError):
    """Raised when a git worktree operation fails."""


def _run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def create(repo_dir: str, branch: str, base: str = "HEAD") -> str:
    """Create a new worktree for `branch` (branched off `base`) and return its absolute path.

    The worktree lives under `<repo_dir>/.wilbur-worktrees/<branch>`, kept
    out of version control (the parent `.gitignore` is updated to exclude
    `.wilbur-worktrees/` if it doesn't already).
    """
    repo_dir = os.path.abspath(repo_dir)
    worktrees_root = os.path.join(repo_dir, ".wilbur-worktrees")
    os.makedirs(worktrees_root, exist_ok=True)

    gitignore_path = os.path.join(repo_dir, ".gitignore")
    ignore_line = ".wilbur-worktrees/"
    existing = ""
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8") as f:
            existing = f.read()
    if ignore_line not in existing.splitlines():
        with open(gitignore_path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(ignore_line + "\n")

    worktree_path = os.path.join(worktrees_root, branch)

    result = _run(
        ["git", "worktree", "add", "-b", branch, worktree_path, base],
        cwd=repo_dir,
    )
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or result.stdout.strip())

    return os.path.abspath(worktree_path)


def remove(repo_dir: str, path: str, branch: str | None = None, force: bool = False) -> None:
    """Remove a worktree (and optionally its branch). Idempotent: never raises if already gone."""
    repo_dir = os.path.abspath(repo_dir)

    args = ["git", "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(path)

    result = _run(args, cwd=repo_dir)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        already_gone = (
            "is not a working tree" in stderr
            or "No such file or directory" in stderr
            or not os.path.exists(path)
        )
        if not already_gone:
            raise WorktreeError(stderr or result.stdout.strip())

    # Prune stale worktree metadata regardless (best-effort, never fatal).
    _run(["git", "worktree", "prune"], cwd=repo_dir)

    if branch:
        delete_flag = "-D" if force else "-d"
        branch_result = _run(["git", "branch", delete_flag, branch], cwd=repo_dir)
        if branch_result.returncode != 0:
            stderr = branch_result.stderr.strip()
            branch_already_gone = "not found" in stderr or "invalid" in stderr.lower()
            if not branch_already_gone:
                raise WorktreeError(stderr or branch_result.stdout.strip())


def merge(repo_dir: str, branch: str, message: str | None = None) -> str:
    """Merge `branch` into whatever branch is currently checked out in `repo_dir`.

    Returns the resulting merge commit sha. On failure (e.g. a conflict),
    aborts the in-progress merge (best-effort) before raising.
    """
    repo_dir = os.path.abspath(repo_dir)
    commit_message = message or f"Merge branch '{branch}'"

    result = _run(
        ["git", "-C", repo_dir, "merge", "--no-ff", branch, "-m", commit_message],
    )
    if result.returncode != 0:
        _run(["git", "-C", repo_dir, "merge", "--abort"])
        raise WorktreeError(result.stderr.strip() or result.stdout.strip())

    sha_result = _run(["git", "-C", repo_dir, "rev-parse", "HEAD"])
    if sha_result.returncode != 0:
        raise WorktreeError(sha_result.stderr.strip() or sha_result.stdout.strip())

    return sha_result.stdout.strip()


def diff(repo_dir: str, branch: str, base: str = "HEAD") -> str:
    """Return the raw `git diff <base>...<branch>` text, for a reviewer to read."""
    repo_dir = os.path.abspath(repo_dir)

    result = _run(["git", "-C", repo_dir, "diff", f"{base}...{branch}"])
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or result.stdout.strip())

    return result.stdout
