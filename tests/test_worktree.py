import os
import subprocess

import pytest

from wilbur import worktree


def _git(args, cwd):
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo_dir = str(repo_dir)

    _git(["init", "-b", "main"], repo_dir)
    _git(["config", "user.email", "test@example.com"], repo_dir)
    _git(["config", "user.name", "Test"], repo_dir)

    (tmp_path / "repo" / "README.md").write_text("hello\n")
    _git(["add", "README.md"], repo_dir)
    _git(["commit", "-m", "initial commit"], repo_dir)

    return repo_dir


def test_create_makes_worktree_with_base_files(repo):
    path = worktree.create(repo, "feature-a")
    assert os.path.isdir(path)
    assert os.path.exists(os.path.join(path, "README.md"))
    assert os.path.isabs(path)


def test_create_duplicate_branch_raises(repo):
    worktree.create(repo, "feature-b")
    with pytest.raises(worktree.WorktreeError):
        worktree.create(repo, "feature-b")


def test_merge_brings_commit_into_main(repo):
    path = worktree.create(repo, "feature-c")

    new_file = os.path.join(path, "new.txt")
    with open(new_file, "w", encoding="utf-8") as f:
        f.write("new content\n")

    _git(["add", "new.txt"], path)
    _git(["commit", "-m", "add new.txt"], path)

    sha = worktree.merge(repo, "feature-c")

    log_output = _git(["log", "--format=%H"], repo)
    assert sha in log_output.splitlines()
    assert os.path.exists(os.path.join(repo, "new.txt"))


def test_remove_cleans_up_and_is_idempotent(repo):
    path = worktree.create(repo, "feature-d")

    new_file = os.path.join(path, "new.txt")
    with open(new_file, "w", encoding="utf-8") as f:
        f.write("content\n")
    _git(["add", "new.txt"], path)
    _git(["commit", "-m", "add new.txt"], path)

    worktree.merge(repo, "feature-d")

    worktree.remove(repo, path, branch="feature-d")
    assert not os.path.exists(path)

    # Idempotent: calling again does not raise.
    worktree.remove(repo, path, branch="feature-d")


def test_diff_reflects_branch_change(repo):
    path = worktree.create(repo, "feature-e")

    new_file = os.path.join(path, "new.txt")
    with open(new_file, "w", encoding="utf-8") as f:
        f.write("diff me\n")
    _git(["add", "new.txt"], path)
    _git(["commit", "-m", "add new.txt"], path)

    diff_text = worktree.diff(repo, "feature-e")
    assert diff_text.strip() != ""
    assert "new.txt" in diff_text
    assert "diff me" in diff_text
