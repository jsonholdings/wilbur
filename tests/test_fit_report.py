"""fit_report() in wilbur/models.py: the verdict for a model + KV cache
against a card's *free* VRAM.

`fit_report` is pure -- it takes `free_mb` as an argument and never calls
`gpu_vram_mb` itself -- so these tests build a `ModelInfo` by hand and pass
free VRAM directly. No nvidia-smi, no network, no GPU.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wilbur.models import ModelInfo, fit_report


def model(size_gb: float) -> ModelInfo:
    return ModelInfo(name="m", size_bytes=int(size_gb * 1e9), capabilities=("tools",))


def test_fits_when_free_vram_covers_model_and_kv():
    """22.0GB model + ~0.9GB KV at 65536 fits a 24GB card with room to spare."""
    fits, why = fit_report(model(22.0), 65536, 24576)
    assert fits
    assert "22.9GB" in why
    assert "available VRAM" in why


def test_does_not_fit_when_free_vram_is_short():
    """22.0GB + ~1.8GB KV at 131072 exceeds a 24GB card."""
    fits, why = fit_report(model(22.0), 131072, 24576)
    assert not fits
    assert "spill to CPU" in why


def test_unknown_free_vram_is_not_a_refusal():
    """free_mb=None means no GPU was detected: the check cannot decide, so it
    passes rather than blocking a CPU-only host."""
    fits, why = fit_report(model(22.0), 65536, None)
    assert fits
    assert "cannot check fit" in why


def test_small_model_fits_a_small_card():
    """A 1GB model fits a 4GB card even at a large context."""
    fits, why = fit_report(model(1.0), 131072, 4096)
    assert fits
    assert "available VRAM" in why


def test_refusal_mentions_contention_when_nothing_is_reclaimable():
    """Only 6GB free and nothing of it ours to reclaim: the hint should name
    a second process holding the card."""
    fits, why = fit_report(model(22.0), 65536, 6144, reclaimable_mb=0)
    assert not fits
    assert "another process holds the card" in why
