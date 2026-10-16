"""generate_image: opt-in, gpu-queue only, fake backends (no GPU in tests)."""
import base64

import pytest

from wilbur.config import Config
from wilbur.tools.base import Approval, ToolContext
from wilbur.tools import image as image_mod
from wilbur.tools.image import GenerateImage, ImageBackendError

PNG = image_mod.PNG_MAGIC + b"fakepixels"


class FakeBackend:
    def __init__(self, result=None, exc=None):
        self.result = result if result is not None else {"png_b64": base64.b64encode(PNG).decode()}
        self.exc = exc
        self.calls = []

    def generate(self, params):
        self.calls.append(params)
        if self.exc:
            raise self.exc
        return self.result


def _ctx(tmp_path, approve=lambda *_: Approval.GRANTED, **cfg):
    config = Config()
    for k, v in cfg.items():
        setattr(config, k, v)
    return ToolContext(cwd=str(tmp_path), config=config, read_files={}, approve=approve)


def test_off_by_default(make_agent):
    agent, _ = make_agent([])
    assert "generate_image" not in agent.registry
    on, _ = make_agent([], image_generation=True)
    assert "generate_image" in on.registry


def test_happy_path_writes_png_with_default_model(tmp_path):
    backend = FakeBackend()
    res = GenerateImage(backend).run({"prompt": "a barn", "path": "out/barn.png"}, _ctx(tmp_path))
    assert not res.is_error, res.output
    assert (tmp_path / "out/barn.png").read_bytes() == PNG
    assert backend.calls[0]["model"] == "qwen-image-2512"


def test_research_model_warns_in_approval_and_result(tmp_path):
    seen = []
    ctx = _ctx(tmp_path, approve=lambda t, d: seen.append(d) or Approval.GRANTED,
               image_model="qwen-image-2.1")
    res = GenerateImage(FakeBackend()).run({"prompt": "x", "path": "a.png"}, ctx)
    assert "research use only" in seen[0] and "research use only" in res.output


def test_unregistered_kind_is_a_clear_error_and_writes_nothing(tmp_path):
    backend = FakeBackend(exc=ImageBackendError("gpu-queue has no 'image_generate' kind"))
    res = GenerateImage(backend).run({"prompt": "x", "path": "a.png"}, _ctx(tmp_path))
    assert res.is_error and "image_generate" in res.output
    assert not (tmp_path / "a.png").exists()


@pytest.mark.parametrize("args,cfg", [
    ({"prompt": "x", "path": "../escape.png"}, {}),
    ({"prompt": "x", "path": "a.jpg"}, {}),
    ({"prompt": "", "path": "a.png"}, {}),
    ({"prompt": "x", "path": "a.png", "width": 99999}, {}),
    ({"prompt": "x", "path": "a.png"}, {"image_model": "sdxl"}),
])
def test_bad_input_is_refused_before_any_gpu_job(tmp_path, args, cfg):
    backend = FakeBackend()
    res = GenerateImage(backend).run(args, _ctx(tmp_path, **cfg))
    assert res.is_error and backend.calls == []


def test_denied_approval_submits_nothing(tmp_path):
    backend = FakeBackend()
    res = GenerateImage(backend).run({"prompt": "x", "path": "a.png"},
                                     _ctx(tmp_path, approve=lambda *_: Approval.DENIED))
    assert res.is_error and backend.calls == []


def test_non_png_result_is_rejected(tmp_path):
    backend = FakeBackend(result={"png_b64": base64.b64encode(b"GIF89a").decode()})
    res = GenerateImage(backend).run({"prompt": "x", "path": "a.png"}, _ctx(tmp_path))
    assert res.is_error and not (tmp_path / "a.png").exists()


def test_gpu_queue_backend_maps_unregistered_kind(monkeypatch):
    gq = pytest.importorskip("jh_gpu_queue_client.client")  # private-build package; skipped in the public mirror

    def refuse(*a, **kw):
        raise gq.UnregisteredKind("unregistered kind image_generate")
    monkeypatch.setattr(gq, "submit_job", refuse)
    with pytest.raises(ImageBackendError, match="no 'image_generate' kind"):
        image_mod.GpuQueueImageBackend().generate({"prompt": "x"})


def test_gpu_queue_backend_returns_result(monkeypatch):
    gq = pytest.importorskip("jh_gpu_queue_client.client")  # private-build package; skipped in the public mirror
    submitted = {}
    monkeypatch.setattr(gq, "submit_job",
                        lambda kind, params, **kw: submitted.update(kind=kind, **kw) or "j1")
    monkeypatch.setattr(gq, "poll", lambda job_id, **kw: {"png_b64": "AA=="})
    assert image_mod.GpuQueueImageBackend().generate({"prompt": "x"}) == {"png_b64": "AA=="}
    assert submitted["kind"] == "image_generate" and submitted["app"] == "wilbur"
