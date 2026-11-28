"""BACKLOG tool-multi-patch: apply_patch, all or nothing across several files."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from wilbur.config import Config
from wilbur.tools import build_registry, diagnostics, patch as patch_mod
from wilbur.tools.base import EDIT_TOOLS, Approval, ToolContext
from wilbur.tools.patch import ApplyPatch


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _ctx(proj: Path, approvals: list | None = None, **cfg) -> ToolContext:
    config = Config()
    config.diagnostics_on_edit = False
    for key, value in cfg.items():
        setattr(config, key, value)

    def approve(tool, detail):
        if approvals is not None:
            approvals.append((tool, detail))
        return Approval.GRANTED
    return ToolContext(cwd=str(proj), config=config, read_files={}, approve=approve)


def _seed(proj: Path, ctx: ToolContext, files: dict[str, str]) -> None:
    for name, text in files.items():
        f = proj / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        ctx.read_files[str(f)] = f.stat().st_mtime


def _snapshot(proj: Path) -> dict[str, str]:
    return {str(p.relative_to(proj)): p.read_text() for p in sorted(proj.rglob("*")) if p.is_file()}


TWO_FILE_DIFF = """\
--- a/a.py
+++ b/a.py
@@ -1,3 +1,3 @@
 def one():
-    return 1
+    return 10

--- a/pkg/b.py
+++ b/pkg/b.py
@@ -1,2 +1,3 @@
 import a
+import os
 print(a.one())
"""


def test_multi_file_unified_diff_applies(proj):
    approvals: list = []
    ctx = _ctx(proj, approvals)
    _seed(proj, ctx, {"a.py": "def one():\n    return 1\n\n", "pkg/b.py": "import a\nprint(a.one())\n"})
    res = ApplyPatch().run({"patch": TWO_FILE_DIFF}, ctx)
    assert not res.is_error, res.output
    assert (proj / "a.py").read_text() == "def one():\n    return 10\n\n"
    assert (proj / "pkg/b.py").read_text() == "import a\nimport os\nprint(a.one())\n"
    assert len(approvals) == 1 and approvals[0][0] == "apply_patch"
    assert "edit a.py" in approvals[0][1] and "edit pkg/b.py" in approvals[0][1]
    # read_files tracks the new mtimes, so a follow-up edit is not refused
    assert ctx.read_files[str(proj / "a.py")] == (proj / "a.py").stat().st_mtime


def test_edit_list_across_files_applies(proj):
    ctx = _ctx(proj)
    _seed(proj, ctx, {"x.txt": "alpha beta\n", "y.txt": "beta beta\n"})
    res = ApplyPatch().run({"edits": [
        {"path": "x.txt", "old_string": "beta", "new_string": "gamma"},
        {"path": "y.txt", "old_string": "beta", "new_string": "gamma", "replace_all": True},
        {"path": "x.txt", "old_string": "alpha", "new_string": "delta"},
    ]}, ctx)
    assert not res.is_error, res.output
    assert (proj / "x.txt").read_text() == "delta gamma\n"
    assert (proj / "y.txt").read_text() == "gamma gamma\n"


def test_one_bad_hunk_changes_no_file(proj):
    ctx = _ctx(proj)
    _seed(proj, ctx, {"a.py": "def one():\n    return 1\n\n", "pkg/b.py": "import a\nprint(a.one())\n"})
    before = _snapshot(proj)
    bad = TWO_FILE_DIFF.replace(" print(a.one())", " print(a.two())")
    res = ApplyPatch().run({"patch": bad}, ctx)
    assert res.is_error
    assert _snapshot(proj) == before
    # the reply names the failing file and hunk
    assert "no file was changed" in res.output
    assert "pkg/b.py hunk 1" in res.output and "not found" in res.output


def test_failure_mid_commit_rolls_back_written_files(proj, monkeypatch):
    ctx = _ctx(proj)
    _seed(proj, ctx, {"a.txt": "one\n", "b.txt": "two\n", "c.txt": "three\n"})
    before = _snapshot(proj)
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk full (injected)")
        return real_replace(src, dst)
    monkeypatch.setattr(patch_mod.os, "replace", flaky_replace)
    res = ApplyPatch().run({"edits": [
        {"path": "a.txt", "old_string": "one", "new_string": "ONE"},
        {"path": "b.txt", "delete": True},
        {"path": "c.txt", "old_string": "three", "new_string": "THREE"},
        {"path": "d.txt", "content": "new\n"},
    ]}, ctx)
    monkeypatch.setattr(patch_mod.os, "replace", real_replace)
    assert res.is_error and "restored" in res.output
    assert _snapshot(proj) == before
    assert not list(proj.glob(".*patch-tmp"))


def test_paths_outside_project_are_refused(proj, tmp_path):
    approvals: list = []
    ctx = _ctx(proj, approvals)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    ctx.read_files[str(outside)] = outside.stat().st_mtime
    _seed(proj, ctx, {"in.txt": "x\n"})
    for edits in ([{"path": str(outside), "old_string": "keep", "new_string": "gone"}],
                  [{"path": "../outside.txt", "old_string": "keep", "new_string": "gone"}],
                  [{"path": "in.txt", "old_string": "x", "new_string": "y"},
                   {"path": "../escape.txt", "content": "evil\n"}]):
        res = ApplyPatch().run({"edits": edits}, ctx)
        assert res.is_error and "outside the working directory" in res.output
    diff = "--- /dev/null\n+++ b/../escape2.txt\n@@ -0,0 +1 @@\n+evil\n"
    assert ApplyPatch().run({"patch": diff}, ctx).is_error
    assert outside.read_text() == "keep\n"
    assert (proj / "in.txt").read_text() == "x\n"
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "escape2.txt").exists()
    assert approvals == []   # refused before anyone was asked


def test_create_and_delete_file_hunks(proj):
    ctx = _ctx(proj)
    _seed(proj, ctx, {"old.txt": "bye\nnow\n"})
    diff = ("diff --git a/new/n.txt b/new/n.txt\nnew file mode 100644\n"
            "--- /dev/null\n+++ b/new/n.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n"
            "diff --git a/old.txt b/old.txt\ndeleted file mode 100644\n"
            "--- a/old.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-bye\n-now\n")
    res = ApplyPatch().run({"patch": diff}, ctx)
    assert not res.is_error, res.output
    assert (proj / "new/n.txt").read_text() == "hello\nworld\n"
    assert not (proj / "old.txt").exists()
    assert str(proj / "old.txt") not in ctx.read_files
    assert "create new/n.txt" in res.output and "delete old.txt" in res.output
    # a create hunk for a file that already exists is refused
    again = ApplyPatch().run({"patch": "--- /dev/null\n+++ b/new/n.txt\n@@ -0,0 +1 @@\n+x\n"}, ctx)
    assert again.is_error and "already exists" in again.output


def test_new_file_mode_follows_umask_not_mkstemp(proj):
    ctx = _ctx(proj)
    old = os.umask(0o022)
    try:
        ApplyPatch().run({"edits": [{"path": "m.txt", "content": "x\n"}]}, ctx)
    finally:
        os.umask(old)
    assert (proj / "m.txt").stat().st_mode & 0o777 == 0o644


def test_existing_file_keeps_its_mode(proj):
    ctx = _ctx(proj)
    _seed(proj, ctx, {"run.sh": "echo a\n"})
    (proj / "run.sh").chmod(0o755)
    ctx.read_files[str(proj / "run.sh")] = (proj / "run.sh").stat().st_mtime
    res = ApplyPatch().run({"edits": [{"path": "run.sh", "old_string": "a", "new_string": "b"}]}, ctx)
    assert not res.is_error
    assert (proj / "run.sh").stat().st_mode & 0o777 == 0o755


def test_whitespace_tolerant_context_matching(proj):
    ctx = _ctx(proj)
    _seed(proj, ctx, {"t.py": "def f():\n\tif x:\n\t\treturn 1\n"})
    # the model's context uses spaces where the file has tabs
    diff = "--- a/t.py\n+++ b/t.py\n@@ -2,2 +2,2 @@\n     if x:\n-        return 1\n+\t\treturn 2\n"
    res = ApplyPatch().run({"patch": diff}, ctx)
    assert not res.is_error, res.output
    assert (proj / "t.py").read_text() == "def f():\n\tif x:\n\t\treturn 2\n"


def test_tolerant_match_must_be_unique(proj):
    ctx = _ctx(proj)
    _seed(proj, ctx, {"t.py": "a = 1\nb  =  2\nc = 3\nb = 2\n"})
    before = _snapshot(proj)
    diff = "--- a/t.py\n+++ b/t.py\n@@ -2 +2 @@\n-b   =   2\n+b = 9\n"
    res = ApplyPatch().run({"patch": diff}, ctx)
    assert res.is_error and "2 places" in res.output and "t.py hunk 1" in res.output
    assert _snapshot(proj) == before


def test_exact_ambiguity_resolved_by_header_line_else_refused(proj):
    ctx = _ctx(proj)
    _seed(proj, ctx, {"r.txt": "x\ny\nx\ny\n"})
    ok = ApplyPatch().run({"patch": "--- a/r.txt\n+++ b/r.txt\n@@ -3,2 +3,2 @@\n x\n-y\n+Y\n"}, ctx)
    assert not ok.is_error, ok.output
    assert (proj / "r.txt").read_text() == "x\ny\nx\nY\n"
    _seed(proj, ctx, {"s.txt": "x\ny\nx\ny\n"})
    bad = ApplyPatch().run({"patch": "--- a/s.txt\n+++ b/s.txt\n@@ -9,2 +9,2 @@\n x\n-y\n+Y\n"}, ctx)
    assert bad.is_error and "2 places" in bad.output


def test_unread_or_changed_files_are_refused(proj):
    ctx = _ctx(proj)
    (proj / "u.txt").write_text("u\n")
    res = ApplyPatch().run({"edits": [{"path": "u.txt", "old_string": "u", "new_string": "v"}]}, ctx)
    assert res.is_error and "read it before patching" in res.output
    ctx.read_files[str(proj / "u.txt")] = (proj / "u.txt").stat().st_mtime - 5
    res = ApplyPatch().run({"edits": [{"path": "u.txt", "old_string": "u", "new_string": "v"}]}, ctx)
    assert res.is_error and "changed on disk" in res.output
    assert (proj / "u.txt").read_text() == "u\n"


def test_denied_approval_changes_nothing(proj):
    ctx = _ctx(proj)
    ctx.approve = lambda tool, detail: Approval.DENIED
    _seed(proj, ctx, {"a.txt": "a\n"})
    res = ApplyPatch().run({"edits": [{"path": "a.txt", "old_string": "a", "new_string": "b"}]}, ctx)
    assert res.is_error and "did not run" in res.output
    assert (proj / "a.txt").read_text() == "a\n"


def test_diagnostics_run_on_every_touched_file(proj, monkeypatch):
    seen: list[str] = []

    def fake_diagnose(path, cwd, config, text=None, runner=None):
        seen.append(Path(path).name)
        return f"[diagnostics: {Path(path).name}: checked]"
    monkeypatch.setattr(diagnostics, "diagnose", fake_diagnose)
    ctx = _ctx(proj, diagnostics_on_edit=True)
    _seed(proj, ctx, {"a.py": "def one():\n    return 1\n\n", "pkg/b.py": "import a\nprint(a.one())\n",
                      "gone.py": "x = 1\n"})
    res = ApplyPatch().run({"patch": TWO_FILE_DIFF + "--- a/gone.py\n+++ /dev/null\n"}, ctx)
    assert not res.is_error, res.output
    assert sorted(seen) == ["a.py", "b.py"]        # the deleted file is not checked
    assert "[diagnostics: a.py: checked]" in res.output
    assert "[diagnostics: b.py: checked]" in res.output


def test_switch_off_means_tool_not_offered():
    on = Config()
    off = Config()
    off.apply_patch_tool = False
    assert "apply_patch" in build_registry(config=on)
    assert "apply_patch" in build_registry()
    assert "apply_patch" not in build_registry(config=off)
    assert "edit_file" in build_registry(config=off)


def test_auto_edit_covers_apply_patch():
    assert "apply_patch" in EDIT_TOOLS


def test_needs_exactly_one_input_form(proj):
    ctx = _ctx(proj)
    assert ApplyPatch().run({}, ctx).is_error
    assert ApplyPatch().run({"patch": "x", "edits": [{"path": "a"}]}, ctx).is_error
    res = ApplyPatch().run({"patch": "just words"}, ctx)
    assert res.is_error and "no file headers" in res.output
