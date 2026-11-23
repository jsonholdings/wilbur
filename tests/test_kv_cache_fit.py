"""2.51.0 KV-aware fit: KV size from /api/show model_info, checked against
llama.cpp load logs (f16, num_ctx 65536, 2026-11-23): 35b-a3b 1280 MiB, 27b
4096 MiB. Fixtures are the real model_info (tokenizer/vision keys dropped)."""
import json
from pathlib import Path

import pytest

from wilbur.models import ModelInfo, fit_report, kv_cache_gb, model_need_gb

INFO = json.loads((Path(__file__).parent / "fixtures" / "kv_model_info.json").read_text())
B35 = "huihui_ai/Qwen3.6-abliterated:35b-a3b-q8_0"
B27 = "huihui_ai/qwen3.8-abliterated:27b"
SIZES = {B35: 38696967113, B27: 17741871954}  # /api/tags sizes
MiB = 1024 * 1024
ONE_CARD, TWO_CARDS = 24576, 49152


def model(name, with_meta=True):
    return ModelInfo(name=name, size_bytes=SIZES[name],
                     model_info=INFO[name] if with_meta else {})


@pytest.mark.parametrize("name,measured_mib", [(B35, 1280), (B27, 4096)])
def test_f16_kv_matches_llama_cpp_log(name, measured_mib):
    gb, from_meta = kv_cache_gb(model(name), 65536, "f16")
    assert from_meta
    assert abs(gb * 1e9 / MiB - measured_mib) / measured_mib < 0.10


def test_q8_and_q4_scale():
    f16 = kv_cache_gb(model(B27), 65536, "f16")[0]
    assert kv_cache_gb(model(B27), 65536, "q8_0")[0] == pytest.approx(f16 / 2, rel=0.1)
    assert kv_cache_gb(model(B27), 65536, "q4_0")[0] == pytest.approx(f16 / 4, rel=0.15)


def test_27b_131k_q8_fits_one_card():
    fits, why = fit_report(model(B27), 131072, ONE_CARD, kv_cache_type="q8_0")
    assert fits, why
    assert "q8_0" in why


def test_27b_131k_f16_does_not_fit_one_card():
    fits, why = fit_report(model(B27), 131072, ONE_CARD, kv_cache_type="f16")
    assert not fits, why


def test_35b_131k_q8_fits_two_cards():
    assert not fit_report(model(B35), 131072, ONE_CARD, kv_cache_type="q8_0")[0]
    fits, why = fit_report(model(B35), 131072, TWO_CARDS, kv_cache_type="q8_0")
    assert fits, why


def test_missing_metadata_falls_back_and_says_so():
    gb, from_meta = kv_cache_gb(model(B27, with_meta=False), 65536, "f16")
    assert not from_meta and gb == pytest.approx(0.9)
    _, why = fit_report(model(B27, with_meta=False), 65536, ONE_CARD)
    assert "heuristic" in why


def test_control_old_heuristic_underestimates_27b():
    old = model_need_gb(model(B27, with_meta=False), 65536)
    new = model_need_gb(model(B27), 65536, "f16")
    measured = SIZES[B27] / 1e9 + 4096 * MiB / 1e9
    assert old < measured * 0.85
    assert new == pytest.approx(measured, rel=0.02)
    # The same model at 131072 f16: the old number says it fits, the real one does not.
    assert fit_report(model(B27, with_meta=False), 131072, ONE_CARD)[0] is True
    assert fit_report(model(B27), 131072, ONE_CARD, kv_cache_type="f16")[0] is False


def test_bad_kv_cache_type_rejected(tmp_path, monkeypatch):
    from wilbur import config as cfgmod
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"kv_cache_type": "q5_1"}))
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", p)
    with pytest.raises(ValueError):
        cfgmod.Config.load()
