"""nvidia-smi output parsing for wilbur.models.

`all_gpu_vram_mb()` shells out to `nvidia-smi` (falling back to
`flatpak-spawn --host nvidia-smi`); `gpu_vram_mb()` and `gpu_free_mb()`
pick one card out of that list by index. These tests monkeypatch
`subprocess.run` so no real nvidia-smi -- and no GPU -- is ever touched.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur.models import all_gpu_vram_mb, gpu_free_mb, gpu_vram_mb


def fake_smi(monkeypatch, stdout: str, returncode: int = 0):
    """Make every nvidia-smi invocation (direct and flatpak fallback)
    return the same scripted output."""
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")
    monkeypatch.setattr(subprocess, "run", run)


def test_two_gpus_parsed_in_index_order(monkeypatch):
    """One row per card, in index order; per-card lookups read their own
    row, never a pooled sum across cards."""
    fake_smi(monkeypatch, "20000, 24576\n2048, 24576\n")
    assert all_gpu_vram_mb() == [(20000, 24576), (2048, 24576)]
    assert gpu_vram_mb(0) == (20000, 24576)
    assert gpu_vram_mb(1) == (2048, 24576)
    assert gpu_free_mb(0) == 20000
    assert gpu_free_mb(1) == 2048
    # Free, not total, and not the sum of both cards.
    assert gpu_free_mb(0) != 20000 + 2048
    assert gpu_free_mb(1) != 24576


def test_index_out_of_range_returns_none(monkeypatch):
    """An index past the last card (or negative) names a card that is not
    present, so both lookups report None rather than a wrong card's VRAM."""
    fake_smi(monkeypatch, "20000, 24576\n2048, 24576\n")
    assert gpu_vram_mb(2) is None
    assert gpu_free_mb(2) is None
    assert gpu_vram_mb(-1) is None
    assert gpu_free_mb(-1) is None


def test_missing_nvidia_smi_returns_empty(monkeypatch):
    """nvidia-smi not on PATH (both the direct and flatpak invocations
    raise) means no GPU is reachable: [] and None, not an exception."""
    def boom(cmd, **kwargs):
        raise OSError("nvidia-smi: not found")
    monkeypatch.setattr(subprocess, "run", boom)
    assert all_gpu_vram_mb() == []
    assert gpu_vram_mb() is None
    assert gpu_free_mb() is None


def test_failed_nvidia_smi_returns_empty(monkeypatch):
    """A non-zero exit with no usable output is the same as missing."""
    fake_smi(monkeypatch, "", returncode=9)
    assert all_gpu_vram_mb() == []
    assert gpu_vram_mb(0) is None
    assert gpu_free_mb(0) is None


def test_flatpak_fallback_used_when_direct_fails(monkeypatch):
    """The direct invocation fails but the flatpak-spawn fallback succeeds:
    the cards are still reported."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "nvidia-smi":
            raise OSError("nvidia-smi: not found")
        return subprocess.CompletedProcess(cmd, 0, "18432, 24576\n", "")
    monkeypatch.setattr(subprocess, "run", run)
    assert all_gpu_vram_mb() == [(18432, 24576)]
    assert gpu_vram_mb(0) == (18432, 24576)
    assert gpu_free_mb(0) == 18432
    assert any(c[0] == "flatpak-spawn" for c in calls)
