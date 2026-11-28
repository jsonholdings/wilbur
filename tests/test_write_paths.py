"""2.75.0: apply_patch is a write tool that names several paths.

context.written_paths lists them so the agent counts the writes, tracks
untested code writes, and compaction treats a patched file as acted on.
"""
from __future__ import annotations

from wilbur.agent import Agent, WRITE_TOOLS
from wilbur.context import written_paths
from wilbur.llm import ToolCall

DIFF = """--- a/one.py
+++ b/one.py
@@ -1 +1 @@
-a = 1
+a = 2
--- /dev/null
+++ b/two.py
@@ -0,0 +1 @@
+b = 1
"""


def test_apply_patch_is_a_write_tool():
    assert {"write_file", "edit_file", "apply_patch"} <= WRITE_TOOLS


def test_single_path_tools():
    assert written_paths("edit_file", {"path": "x.py"}) == ["x.py"]
    assert written_paths("write_file", {"path": "y.md", "content": ""}) == ["y.md"]


def test_patch_paths_from_diff_headers():
    assert written_paths("apply_patch", {"patch": DIFF}) == ["one.py", "two.py"]


def test_patch_paths_from_edit_list_deduplicated():
    edits = [{"path": "a.py", "old_string": "1", "new_string": "2"},
             {"path": "a.py", "old_string": "3", "new_string": "4"},
             {"path": "b.py", "delete": True}]
    assert written_paths("apply_patch", {"edits": edits}) == ["a.py", "b.py"]


def test_controls_names_nothing():
    # Control: a read tool, a diff that does not parse, and junk edits name no path.
    assert written_paths("read_file", {"path": "x.py"}) == []
    assert written_paths("apply_patch", {"patch": "not a diff"}) == []
    assert written_paths("apply_patch", {"edits": ["junk", {"old_string": "x"}]}) == []


def test_distinct_patches_have_distinct_signatures():
    one = ToolCall(name="apply_patch", arguments={"patch": DIFF})
    two = ToolCall(name="apply_patch", arguments={"patch": DIFF.replace("a = 2", "a = 3")})
    assert Agent._arg_signature(one) != Agent._arg_signature(two)
    assert Agent._arg_signature(one) == Agent._arg_signature(
        ToolCall(name="apply_patch", arguments={"patch": DIFF}))
