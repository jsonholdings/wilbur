"""Opt-in `generate_image` tool: text-to-image through gpu-queue, never the GPU directly.

Off by default (`Config.image_generation`). The tool only submits a job of
kind `IMAGE_KIND` to gpu-queue and waits for its result; the queue owns the
GPU and the model. gpu-queue has no image kind yet (requested via handoff
2026-10-16), so until it does, submission fails with a clear "unregistered
kind" error instead of anything running.

Job contract (what the gpu-queue kind must implement):
    params: {"prompt": str, "model": str, "width": int, "height": int,
             "seed": int | None, "negative_prompt": str}
    result: {"png_b64": str}  -- base64 PNG bytes
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Protocol

from .base import Approval, ToolContext, ToolResult, denial_message

IMAGE_KIND = "image_generate"
DEFAULT_IMAGE_MODEL = "qwen-image-2512"
# Licence notes shown to the agent and the user. 2512 is Apache-2.0; 2.1 is
# allowed but carries a research-only licence, so it is never the default.
IMAGE_MODELS = {
    "qwen-image-2512": None,
    "qwen-image-2.1": ("qwen-image-2.1 is licensed for research use only; "
                       "do not use its output commercially."),
}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_SIDE = 2048


class ImageBackendError(RuntimeError):
    pass


class ImageBackend(Protocol):
    def generate(self, params: dict[str, Any]) -> dict[str, Any]: ...


class GpuQueueImageBackend:
    """Submits `IMAGE_KIND` to gpu-queue and polls it to completion."""

    def __init__(self, max_wait: float = 1200.0) -> None:
        self.max_wait = max_wait

    def generate(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            from jh_gpu_queue_client import client as gq
        except ImportError as exc:
            raise ImageBackendError(
                "jh-gpu-queue-client is not installed (requirements-gpu-queue.txt)") from exc
        try:
            job_id = gq.submit_job(IMAGE_KIND, params, why="wilbur generate_image",
                                   app="wilbur", process="generate_image")
            record = gq.poll(job_id, max_wait=self.max_wait)
        except gq.UnregisteredKind as exc:
            raise ImageBackendError(
                f"gpu-queue has no '{IMAGE_KIND}' kind registered yet; image "
                "generation is unavailable until it is added") from exc
        except gq.GpuQueueError as exc:
            raise ImageBackendError(str(exc)) from exc
        return record  # poll() returns the finished job's result dict


def default_backend() -> ImageBackend:
    return GpuQueueImageBackend()


class GenerateImage:
    name = "generate_image"
    read_only = False
    description = (
        "Generate a PNG image from a text prompt (queued on the shared GPU; can "
        "take minutes) and save it under the project. Use only when the user "
        "asked for an image."
    )
    schema = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "path": {"type": "string", "description": "where to save the .png"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "seed": {"type": "integer"},
        },
        "required": ["prompt", "path"],
    }

    def __init__(self, backend: ImageBackend | None = None) -> None:
        self.backend = backend

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        model = getattr(ctx.config, "image_model", DEFAULT_IMAGE_MODEL) or DEFAULT_IMAGE_MODEL
        if model not in IMAGE_MODELS:
            return ToolResult(f"Unknown image_model {model!r}; choose one of "
                              f"{', '.join(IMAGE_MODELS)}.", is_error=True)
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return ToolResult("generate_image needs a non-empty prompt.", is_error=True)
        try:
            width = int(args.get("width") or 1024)
            height = int(args.get("height") or 1024)
        except (TypeError, ValueError):
            return ToolResult("width and height must be integers.", is_error=True)
        if not (64 <= width <= MAX_SIDE and 64 <= height <= MAX_SIDE):
            return ToolResult(f"width and height must be 64..{MAX_SIDE}.", is_error=True)
        root = Path(ctx.cwd).resolve()
        out = (root / str(args["path"])).resolve()
        if out.suffix.lower() != ".png":
            return ToolResult("path must end in .png", is_error=True)
        if root not in out.parents:
            return ToolResult(f"Refusing to write outside the project: {out}", is_error=True)
        if out.exists():
            return ToolResult(f"{out} already exists; pick a new path.", is_error=True)
        warning = IMAGE_MODELS[model]
        detail = f"generate a {width}x{height} image with {model} -> {out}"
        if warning:
            detail += f" (WARNING: {warning})"
        outcome = ctx.approve("generate_image", detail)
        if outcome is not Approval.GRANTED:
            return ToolResult(denial_message("image generation", outcome), is_error=True)
        params = {"prompt": prompt, "model": model, "width": width, "height": height,
                  "seed": args.get("seed"), "negative_prompt": ""}
        backend = self.backend or default_backend()
        try:
            result = backend.generate(params)
            data = base64.b64decode(result["png_b64"], validate=True)
        except ImageBackendError as exc:
            return ToolResult(f"Image generation failed: {exc}", is_error=True)
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(f"Image backend returned no usable PNG ({exc}).", is_error=True)
        if not data.startswith(PNG_MAGIC):
            return ToolResult("Image backend returned data that is not a PNG.", is_error=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        msg = f"Saved {out} ({len(data)} bytes, {model}, {width}x{height})."
        if warning:
            msg += f" Note: {warning}"
        return ToolResult(msg)
