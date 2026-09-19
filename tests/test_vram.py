"""VRAM fit checking.

The v1 slowdown these guard against: a second Ollama server pinned the card, the
container's model silently offloaded 42 of 65 layers to CPU, and throughput fell
from 173 tok/s to under 1. A fit check measured against *total* VRAM cannot see
another process holding the card, which is why these tests exist.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur.models import (
    ModelInfo, ModelManager, all_gpu_vram_mb, fit_report, gpu_free_mb, gpu_vram_mb,
)


def model(size_gb: float) -> ModelInfo:
    return ModelInfo(name="m", size_bytes=int(size_gb * 1e9), capabilities=("tools",))


# --------------------------------------------------------------------- #
# nvidia-smi parsing


def fake_smi(monkeypatch, stdout: str, returncode: int = 0):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")
    monkeypatch.setattr(subprocess, "run", run)


def test_gpu_vram_reads_free_and_total(monkeypatch):
    fake_smi(monkeypatch, "18432, 24576\n")
    assert gpu_vram_mb() == (18432, 24576)


def test_gpu_free_mb_is_free_not_total(monkeypatch):
    """The bug this replaces returned total, so a busy card looked empty."""
    fake_smi(monkeypatch, "1024, 24576\n")
    assert gpu_free_mb() == 1024


def test_gpu_vram_none_without_nvidia_smi(monkeypatch):
    def boom(cmd, **kwargs):
        raise OSError("nvidia-smi: not found")
    monkeypatch.setattr(subprocess, "run", boom)
    assert gpu_vram_mb() is None
    assert gpu_free_mb() is None


def test_gpu_vram_none_on_nonzero_exit(monkeypatch):
    fake_smi(monkeypatch, "", returncode=9)
    assert gpu_vram_mb() is None


# --------------------------------------------------------------------- #
# multi-card: each card read individually, never summed


def test_single_card_reports_one_row(monkeypatch):
    fake_smi(monkeypatch, "18432, 24576\n")
    assert all_gpu_vram_mb() == [(18432, 24576)]
    assert gpu_vram_mb(0) == (18432, 24576)


def test_two_cards_read_by_index_not_summed(monkeypatch):
    """A model judged against the card Ollama will place it on, not a pooled
    total across cards -- the exact defect BACKLOG `multi-gpu-vram-accounting`
    describes: reporting 24GB free when 48GB exists, or "fits" when the idle
    card masks a pinned one.
    """
    fake_smi(monkeypatch, "20000, 24576\n2048, 24576\n")
    assert all_gpu_vram_mb() == [(20000, 24576), (2048, 24576)]
    assert gpu_vram_mb(0) == (20000, 24576)
    assert gpu_vram_mb(1) == (2048, 24576)
    assert gpu_free_mb(0) == 20000
    assert gpu_free_mb(1) == 2048
    # Never the sum of both cards.
    assert gpu_free_mb(0) != 20000 + 2048


def test_second_card_pinned_does_not_hide_behind_first_cards_headroom(monkeypatch):
    """Card 0 is nearly idle; card 1 (the configured target) is pinned by
    another process. The fit check must see card 1's real free VRAM, not
    card 0's, or it silently spills exactly like the original v1 failure.
    """
    fake_smi(monkeypatch, "22000, 24576\n1024, 24576\n")
    fits, why = fit_report(model(4.0), 65536, gpu_free_mb(1))
    assert not fits
    assert "another process holds the card" in why


def test_index_past_available_cards_is_none(monkeypatch):
    fake_smi(monkeypatch, "18432, 24576\n")
    assert gpu_vram_mb(1) is None
    assert gpu_free_mb(1) is None


def test_all_gpu_vram_empty_without_nvidia_smi(monkeypatch):
    def boom(cmd, **kwargs):
        raise OSError("nvidia-smi: not found")
    monkeypatch.setattr(subprocess, "run", boom)
    assert all_gpu_vram_mb() == []


# --------------------------------------------------------------------- #
# fit_report


def test_no_gpu_does_not_block():
    fits, why = fit_report(model(22.0), 65536, None)
    assert fits and "cannot check fit" in why


def test_fits_on_an_idle_card():
    fits, why = fit_report(model(22.0), 65536, 24576)
    assert fits
    assert "22.9GB" in why


def test_spills_at_larger_context():
    """22.0GB + ~1.8GB KV at 131072 exceeds a 24GB card."""
    fits, why = fit_report(model(22.0), 131072, 24576)
    assert not fits
    assert "spill to CPU" in why


def test_another_process_holding_the_card_is_refused():
    """The v1 failure: only 6GB free, nothing of it ours to reclaim."""
    fits, why = fit_report(model(22.0), 65536, 6144, reclaimable_mb=0)
    assert not fits
    assert "another process holds the card" in why


def test_vram_we_can_evict_counts_as_available():
    """Our own loaded model is not an obstacle -- unload_all() releases it."""
    fits, why = fit_report(model(22.0), 65536, 2048, reclaimable_mb=22528)
    assert fits
    assert "will evict" in why


def test_reclaimable_does_not_suggest_freeing_another_process():
    """When the shortfall is ours, the hint should be ctx/model size, not contention."""
    fits, why = fit_report(model(22.0), 131072, 1024, reclaimable_mb=4096)
    assert not fits
    assert "another process" not in why
    assert "Lower num_ctx" in why


def test_headroom_margin_is_enforced():
    """0.4GB of headroom is required, so an exact fit is still a refusal."""
    exact = model(24576 / 1024 - 0.9)  # model + KV lands exactly on the card
    fits, _ = fit_report(exact, 65536, 24576)
    assert not fits


# --------------------------------------------------------------------- #
# reclaimable_vram_mb


class FakeManager(ModelManager):
    def __init__(self, loaded):
        super().__init__("http://localhost:11434")
        self._loaded = loaded

    def loaded(self):
        return self._loaded


def test_reclaimable_sums_loaded_models():
    m = FakeManager([{"name": "a", "size_vram": 1024 * 1024 * 1024},
                     {"name": "b", "size_vram": 512 * 1024 * 1024}])
    assert m.reclaimable_vram_mb() == 1536


def test_reclaimable_is_zero_when_nothing_loaded():
    assert FakeManager([]).reclaimable_vram_mb() == 0


def test_reclaimable_ignores_malformed_entries():
    """A model with no size_vram must not crash the fit check."""
    m = FakeManager([{"name": "a"}, {"name": "b", "size_vram": None},
                     {"name": "c", "size_vram": "nonsense"},
                     {"name": "d", "size_vram": 1024 * 1024 * 1024}])
    assert m.reclaimable_vram_mb() == 1024
