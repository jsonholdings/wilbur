"""The shipped contrib queue files must load cleanly with wilbur.loop.load_queue.

No network, no GPU: load_queue only reads JSON from disk.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from wilbur import loop

CONTRIB = Path(__file__).resolve().parents[1] / "contrib"
QUEUE_FILES = [
    CONTRIB / "loop-queue.example.json",
    CONTRIB / "loop-queue.seed.json",
]

if not CONTRIB.is_dir():
    # contrib/ is private-repo only; the public mirror does not ship it.
    pytest.skip("contrib/ not shipped in this checkout", allow_module_level=True)


@pytest.fixture(params=QUEUE_FILES, ids=lambda p: p.name)
def queue_items(request):
    path = request.param
    assert path.is_file(), f"missing queue file: {path}"
    return loop.load_queue(str(path))


def test_loads_with_load_queue(queue_items):
    """Both contrib queue files parse into a non-empty list of QueueItems."""
    assert queue_items, "queue file loaded zero items"
    for item in queue_items:
        assert isinstance(item, loop.QueueItem)
        assert item.id
        assert item.task


def test_every_item_gets_a_wilbur_branch(queue_items):
    for item in queue_items:
        assert item.branch.startswith("wilbur/"), (
            f"item {item.id!r} has branch {item.branch!r}, not a wilbur/ branch")
        assert item.branch != "wilbur/", "branch is the bare wilbur/ prefix"


def test_item_ids_are_unique(queue_items):
    ids = [item.id for item in queue_items]
    assert len(ids) == len(set(ids)), f"duplicate item ids: {sorted(ids)}"
