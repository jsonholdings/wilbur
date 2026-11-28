"""BACKLOG tool-git: structured git tools, path-limited commit, run_bash git guard."""
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools import build_registry
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools.gitguard import default_branch_push, git_refusal


def _run(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key, value in {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                       "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
                       "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("one\ntwo\n")
    (root / "b.txt").write_text("bee\n")
    _run(root, "add", "--", "a.txt", "b.txt")
    _run(root, "commit", "-q", "-m", "first")
    return root


def _ctx(root, approvals=None, answer=Approval.GRANTED, **cfg):
    config = Config()
    for key, value in cfg.items():
        setattr(config, key, value)

    def approve(tool, detail):
        if approvals is not None:
            approvals.append((tool, detail))
        return answer
    return ToolContext(cwd=str(root), config=config, read_files={}, approve=approve)


def _tool(name, ctx, args):
    return build_registry(config=ctx.config)[name].run(args, ctx)


def _data(result):
    assert not result.is_error, result.output
    return json.loads(result.output)


# ------------------------------------------------------------ structured reads

def test_status_is_structured(repo):
    (repo / "a.txt").write_text("one\nTWO\n")
    (repo / "b.txt").write_text("bee2\n")
    _run(repo, "add", "--", "b.txt")
    (repo / "new.txt").write_text("n\n")
    data = _data(_tool("git_status", _ctx(repo), {}))
    assert data["branch"] == "main" and data["clean"] is False
    assert data["staged"] == [{"path": "b.txt", "change": "modified"}]
    assert data["unstaged"] == [{"path": "a.txt", "change": "modified"}]
    assert data["untracked"] == ["new.txt"]


def test_status_clean(repo):
    assert _data(_tool("git_status", _ctx(repo), {}))["clean"] is True


def test_diff_is_structured(repo):
    (repo / "a.txt").write_text("one\nTWO\nthree\n")
    files = _data(_tool("git_diff", _ctx(repo), {}))["files"]
    assert len(files) == 1
    f = files[0]
    assert (f["path"], f["status"], f["added"], f["deleted"]) == ("a.txt", "modified", 2, 1)
    assert f["hunks"][0]["old_start"] == 1 and "-two" in f["hunks"][0]["lines"]
    _run(repo, "add", "--", "a.txt")
    assert _data(_tool("git_diff", _ctx(repo), {}))["files"] == []
    staged = _data(_tool("git_diff", _ctx(repo), {"staged": True}))["files"]
    assert staged[0]["path"] == "a.txt"


def test_diff_new_and_deleted_files(repo):
    (repo / "c.txt").write_text("c\n")
    _run(repo, "add", "--", "c.txt")
    _run(repo, "rm", "-q", "--", "b.txt")
    files = {f["path"]: f["status"] for f in
             _data(_tool("git_diff", _ctx(repo), {"staged": True}))["files"]}
    assert files == {"c.txt": "added", "b.txt": "deleted"}


def test_log_is_structured(repo):
    (repo / "a.txt").write_text("x\n")
    _run(repo, "commit", "-q", "-m", "second", "--", "a.txt")
    commits = _data(_tool("git_log", _ctx(repo), {"limit": 5}))["commits"]
    assert [c["subject"] for c in commits] == ["second", "first"]
    assert len(commits[0]["sha"]) == 40 and commits[0]["author"] == "T"
    only_b = _data(_tool("git_log", _ctx(repo), {"paths": ["b.txt"]}))["commits"]
    assert [c["subject"] for c in only_b] == ["first"]


def test_blame_is_structured(repo):
    (repo / "a.txt").write_text("one\nchanged\n")
    _run(repo, "commit", "-q", "-m", "edit two", "--", "a.txt")
    lines = _data(_tool("git_blame", _ctx(repo), {"path": "a.txt"}))["lines"]
    assert [(l["line"], l["text"], l["summary"]) for l in lines] == [
        (1, "one", "first"), (2, "changed", "edit two")]
    ranged = _data(_tool("git_blame", _ctx(repo), {"path": "a.txt", "start": 2, "end": 2}))
    assert [l["line"] for l in ranged["lines"]] == [2]


def test_blame_refuses_outside_path(repo, tmp_path):
    out = _tool("git_blame", _ctx(repo), {"path": "../outside.txt"})
    assert out.is_error and "outside" in out.output


# ------------------------------------------------------------ commit

def test_commit_only_named_paths_keeps_other_staged_files_staged(repo):
    """The shared-index hazard: another session's staged file must not ride along."""
    (repo / "a.txt").write_text("mine\n")
    (repo / "b.txt").write_text("someone else's staged work\n")
    _run(repo, "add", "--", "b.txt")
    approvals = []
    data = _data(_tool("git_commit", _ctx(repo, approvals),
                       {"paths": ["a.txt"], "message": "my change"}))
    assert data["files"] == [{"change": "M", "path": "a.txt"}]
    assert approvals and approvals[0][0] == "git_commit"
    assert _run(repo, "diff", "--cached", "--name-only").split() == ["b.txt"]
    assert _run(repo, "show", "HEAD:a.txt") == "mine\n"
    assert _run(repo, "show", "HEAD:b.txt") == "bee\n"


def test_commit_adds_a_new_named_file_only(repo):
    (repo / "new.txt").write_text("n\n")
    (repo / "other.txt").write_text("o\n")
    data = _data(_tool("git_commit", _ctx(repo), {"paths": ["new.txt"], "message": "add"}))
    assert data["files"] == [{"change": "A", "path": "new.txt"}]
    assert "other.txt" in _run(repo, "status", "--porcelain")
    assert "?? other.txt" in _run(repo, "status", "--porcelain")


@pytest.mark.parametrize("paths", [[], ["."], [":/"], ["*.txt"], ["-A"], ["--all"],
                                   ["../x"], ["sub/.."]])
def test_commit_refuses_whole_tree_and_outside_paths(repo, paths):
    (repo / "a.txt").write_text("changed\n")
    approvals = []
    out = _tool("git_commit", _ctx(repo, approvals), {"paths": paths, "message": "m"})
    assert out.is_error
    assert approvals == []
    assert _run(repo, "log", "--format=%s") == "first\n"


def test_commit_denied_does_not_commit(repo):
    (repo / "a.txt").write_text("changed\n")
    out = _tool("git_commit", _ctx(repo, answer=Approval.DENIED),
                {"paths": ["a.txt"], "message": "m"})
    assert out.is_error and "did not run" in out.output
    assert _run(repo, "log", "--format=%s") == "first\n"


# ------------------------------------------------------------ push

@pytest.fixture
def remote(repo, tmp_path):
    bare = tmp_path / "remote.git"
    _run(tmp_path, "init", "-q", "--bare", str(bare))
    _run(repo, "remote", "add", "origin", str(bare))
    return bare


def test_push_to_main_asks_under_default_key(repo, remote):
    approvals = []
    out = _tool("git_push", _ctx(repo, approvals, answer=Approval.DENIED), {})
    assert out.is_error
    assert approvals[0][0] == "git_push_default" and "DEFAULT branch origin/main" in approvals[0][1]
    assert _run(remote, "branch") == ""


def test_push_to_feature_branch_uses_plain_key_and_pushes(repo, remote):
    _run(repo, "checkout", "-q", "-b", "feature")
    approvals = []
    _data(_tool("git_push", _ctx(repo, approvals), {}))
    assert approvals[0][0] == "git_push"
    assert "feature" in _run(remote, "branch")


def test_push_refuses_force_and_refspecs(repo, remote):
    for branch in ("+main", "main:main", "--force"):
        approvals = []
        out = _tool("git_push", _ctx(repo, approvals), {"branch": branch})
        assert out.is_error and approvals == []


# ------------------------------------------------------------ run_bash guard

@pytest.mark.parametrize("command", [
    "git add -A", "git add --all", "git add .", "git add -u", "git -C sub add -A",
    "git commit -am wip", "git commit --all -m x", "cd x && git reset --hard HEAD",
    "git reset --hard", "git clean -fd", "git clean -xfd", "git stash", "git stash pop",
    "git stash drop", "git stash push -m x", "env GIT_DIR=x git stash", "git checkout -- .",
    "git restore .", "echo ok; git add -A",
])
def test_run_bash_refuses_destructive_git(repo, command):
    approvals = []
    out = build_registry()["run_bash"].run({"command": command}, _ctx(repo, approvals))
    assert out.is_error and out.output.startswith("Refused without running")
    assert approvals == []


@pytest.mark.parametrize("command", [
    "git add -- a.txt", "git add a.txt b.txt", "git commit -m x -- a.txt", "git stash list",
    "git stash show", "git clean -n", "git clean --dry-run", "git reset --soft HEAD~1",
    "git status", "echo 'git add -A'", "git restore -- a.txt", "git log --all",
])
def test_run_bash_allows_named_and_read_only_forms(command):
    assert git_refusal(command) is None


def test_run_bash_push_to_main_asks_default_key_first(repo, remote):
    approvals = []
    out = build_registry()["run_bash"].run({"command": "git push origin main"},
                                           _ctx(repo, approvals, answer=Approval.DENIED))
    assert out.is_error and approvals[0][0] == "git_push_default"
    assert _run(remote, "branch") == ""


@pytest.mark.parametrize("command,expected", [
    ("git push", "origin main"), ("git push origin", "origin main"),
    ("git push origin HEAD", "origin main"), ("git push origin feat:master", "origin master"),
    ("git push -u origin refs/heads/main", "origin main"), ("git push --all", "every branch"),
    ("git push origin feature", None), ("git status", None), ("echo git push", None),
])
def test_default_branch_push_detection(repo, command, expected):
    got = default_branch_push(command, str(repo))
    assert (got is None) if expected is None else (got or "").startswith(expected)


def test_default_branch_follows_remote_head(repo, remote):
    _run(repo, "checkout", "-q", "-b", "trunk")
    assert default_branch_push("git push origin trunk", str(repo)) is None
    _run(repo, "update-ref", "refs/remotes/origin/trunk", "HEAD")
    _run(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert default_branch_push("git push origin trunk", str(repo)) == "origin trunk"


# ------------------------------------------------------------ switch (control)

GIT_TOOLS = {"git_status", "git_diff", "git_log", "git_blame", "git_commit", "git_push"}


def test_git_tools_offered_by_default():
    assert GIT_TOOLS <= set(build_registry(config=Config()))


def test_switch_off_removes_git_tools_but_keeps_guard(repo):
    config = Config()
    config.git_tools = False
    assert not (GIT_TOOLS & set(build_registry(config=config)))
    out = build_registry(config=config)["run_bash"].run(
        {"command": "git add -A"}, _ctx(repo, git_tools=False))
    assert out.is_error and "Refused" in out.output


# ------------------------------------------------------------ 2.76.0 merge notes

def test_run_bash_push_to_main_asks_once_when_granted(repo, remote):
    """Ask mode asked twice (git_push_default, then run_bash); one answer now
    covers the command. Control: a feature-branch push still asks run_bash."""
    approvals = []
    out = build_registry()["run_bash"].run({"command": "git push -q origin main"},
                                           _ctx(repo, approvals))
    assert not out.is_error, out.output
    assert [a[0] for a in approvals] == ["git_push_default"]
    assert "main" in _run(remote, "branch")
    _run(repo, "checkout", "-q", "-b", "feature")
    approvals.clear()
    build_registry()["run_bash"].run({"command": "git push -q origin feature"},
                                     _ctx(repo, approvals))
    assert [a[0] for a in approvals] == ["run_bash"]


def test_deny_list_refuses_default_push_before_asking(repo, remote):
    approvals = []
    out = build_registry()["run_bash"].run({"command": "git push origin main"},
                                           _ctx(repo, approvals, denied_bash=["git push"]))
    assert out.is_error and "deny-list" in out.output and approvals == []
