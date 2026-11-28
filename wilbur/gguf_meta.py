"""Read GGUF header metadata, so the VRAM fit check works without Ollama.

The openai backend (llama.cpp, vLLM, SGLang) has no `/api/show`, so the fit
check reads the same keys straight from the GGUF file header and feeds them to
`models.fit_report`. Only scalars and short numeric arrays are kept (the
tokenizer's 150k-string arrays are skipped by length, never materialised).
"""
from __future__ import annotations

import io
import struct
from pathlib import Path
from typing import Any, BinaryIO

from .models import ModelInfo, fit_report, kv_cache_gb

_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?",
           10: "<Q", 11: "<q", 12: "<d"}
_STRING, _ARRAY = 8, 9
MAX_KEPT_ARRAY = 4096  # per-layer lists (head_count_kv) are kept; vocab arrays are not


class GGUFError(ValueError):
    pass


def _read(f: BinaryIO, fmt: str) -> Any:
    size = struct.calcsize(fmt)
    data = f.read(size)
    if len(data) != size:
        raise GGUFError("truncated GGUF header")
    return struct.unpack(fmt, data)[0]


def _string(f: BinaryIO) -> str:
    n = _read(f, "<Q")
    data = f.read(n)
    if len(data) != n:
        raise GGUFError("truncated GGUF string")
    return data.decode("utf-8", "replace")


def _value(f: BinaryIO, vtype: int) -> Any:
    if vtype in _SCALAR:
        return _read(f, _SCALAR[vtype])
    if vtype == _STRING:
        return _string(f)
    if vtype == _ARRAY:
        item_type, count = _read(f, "<I"), _read(f, "<Q")
        if item_type in _SCALAR:
            size = struct.calcsize(_SCALAR[item_type])
            if count > MAX_KEPT_ARRAY:
                f.seek(size * count, 1)
                return None
            return [_read(f, _SCALAR[item_type]) for _ in range(count)]
        if item_type == _STRING:
            for _ in range(count):
                f.seek(_read(f, "<Q"), 1)
            return None
        raise GGUFError(f"unsupported GGUF array item type {item_type}")
    raise GGUFError(f"unsupported GGUF value type {vtype}")


def read_metadata(path: str | Path, header: bytes | None = None) -> dict[str, Any]:
    """All header key/values (long arrays dropped). `header` is the file's
    leading bytes when the caller cannot open it directly (Ollama blobs are
    0600 ollama:ollama; the launcher reads them through the container).
    Raises GGUFError/OSError."""
    with (io.BytesIO(header) if header is not None else open(path, "rb")) as f:
        if f.read(4) != b"GGUF":
            raise GGUFError(f"{path}: not a GGUF file")
        version = _read(f, "<I")
        if version < 2:
            raise GGUFError(f"{path}: GGUF v{version} unsupported")
        _read(f, "<Q")  # tensor count
        kv_count = _read(f, "<Q")
        meta: dict[str, Any] = {}
        for _ in range(kv_count):
            key = _string(f)
            value = _value(f, _read(f, "<I"))
            if value is not None:
                meta[key] = value
        return meta


def model_info_from_gguf(path: str | Path, header: bytes | None = None,
                         size_bytes: int | None = None) -> ModelInfo:
    """ModelInfo shaped like Ollama's, from the file alone. key/value_length
    default to embedding_length / head_count when the GGUF omits them
    (qwen2 does), which is how llama.cpp derives the head size."""
    meta = read_metadata(path, header)
    arch = meta.get("general.architecture", "")
    if arch:
        heads, embd = meta.get(f"{arch}.attention.head_count"), meta.get(f"{arch}.embedding_length")
        if isinstance(heads, int) and heads and isinstance(embd, int):
            meta.setdefault(f"{arch}.attention.key_length", embd // heads)
            meta.setdefault(f"{arch}.attention.value_length", embd // heads)
    return ModelInfo(name=str(meta.get("general.name") or Path(path).name),
                     size_bytes=size_bytes if size_bytes is not None else Path(path).stat().st_size,
                     families=(arch,) if arch else (),
                     context_length=int(meta.get(f"{arch}.context_length") or 0),
                     model_info=meta)


def gguf_fit(info: ModelInfo, num_ctx: int, free_mb: int | None, kv_cache_type: str = "q8_0",
             draft: ModelInfo | None = None) -> tuple[bool, str]:
    """`models.fit_report` for a GGUF served by llama.cpp. `free_mb` is the
    free VRAM summed over the cards the server will use; a draft model's file
    size and its KV cache are added to the need (llama.cpp's KV is unified:
    `-c` tokens in total, however many slots share it)."""
    if draft is not None:
        extra = draft.size_bytes + int(kv_cache_gb(draft, num_ctx, kv_cache_type)[0] * 1e9)
        info = ModelInfo(name=info.name, size_bytes=info.size_bytes + extra, families=info.families,
                         context_length=info.context_length, model_info=info.model_info)
    return fit_report(info, num_ctx, free_mb, 0, kv_cache_type)
