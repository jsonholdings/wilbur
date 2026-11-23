"""Model discovery, switching, and VRAM fit checking.

Any model the local Ollama has pulled can drive Wilbur. The two properties
that actually decide whether a model works as an agent are whether it declares
the `tools` capability and whether it fits in VRAM alongside its KV cache --
both are checked here rather than left for the user to discover mid-task.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class ModelInfo:
    name: str
    size_bytes: int
    parameter_size: str = ""
    quantization: str = ""
    families: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    context_length: int = 0

    @property
    def size_gb(self) -> float:
        return self.size_bytes / 1e9

    @property
    def supports_tools(self) -> bool:
        return "tools" in self.capabilities

    @property
    def supports_thinking(self) -> bool:
        return "thinking" in self.capabilities


class ModelManager:
    def __init__(self, base_url: str, timeout: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._detail_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #

    def _get(self, path: str) -> dict[str, Any]:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=self.timeout) as r:
            return json.loads(r.read())

    def _post(self, path: str, body: dict[str, Any], timeout: int | None = None) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return json.loads(r.read())

    # ------------------------------------------------------------------ #

    def available(self) -> bool:
        try:
            self._get("/api/tags")
            return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return False

    def show(self, name: str) -> dict[str, Any]:
        if name not in self._detail_cache:
            try:
                self._detail_cache[name] = self._post("/api/show", {"model": name})
            except Exception:
                self._detail_cache[name] = {}
        return self._detail_cache[name]

    def list_models(self, *, detail: bool = True) -> list[ModelInfo]:
        try:
            data = self._get("/api/tags")
        except Exception:
            return []
        models = []
        for m in data.get("models", []):
            details = m.get("details", {}) or {}
            info = ModelInfo(
                name=m["name"],
                size_bytes=m.get("size", 0),
                parameter_size=details.get("parameter_size", ""),
                quantization=details.get("quantization_level", ""),
                families=tuple(details.get("families") or []),
            )
            if detail:
                shown = self.show(m["name"])
                info.capabilities = tuple(shown.get("capabilities") or [])
                ctx = [v for k, v in (shown.get("model_info") or {}).items()
                       if k.endswith("context_length")]
                info.context_length = max(ctx) if ctx else 0
            models.append(info)
        return sorted(models, key=lambda i: i.name)

    def agent_capable(self) -> list[ModelInfo]:
        """Models that can actually drive the loop -- i.e. declare tool support."""
        return [m for m in self.list_models() if m.supports_tools]

    def loaded(self) -> list[dict[str, Any]]:
        try:
            return self._get("/api/ps").get("models", [])
        except Exception:
            return []

    def reclaimable_vram_mb(self) -> int:
        """VRAM held by models this server will hand back on `unload_all()`.

        Anything else on the card -- another Ollama, a desktop compositor, a
        training job -- is not ours to reclaim and is deliberately excluded.
        """
        total_bytes = 0
        for m in self.loaded():
            try:
                total_bytes += int(m.get("size_vram") or 0)
            except (TypeError, ValueError):
                continue
        return total_bytes // (1024 * 1024)

    def unload_all(self) -> None:
        """Free VRAM before loading a different model.

        Ollama will evict on its own, but only after the new model has already
        failed to fit and been partially offloaded to CPU -- which is the exact
        silent 5x slowdown this project exists to avoid.
        """
        for m in self.loaded():
            try:
                self._post("/api/generate", {"model": m["name"], "keep_alive": 0})
            except Exception:
                pass

    def pull(self, name: str, on_progress=None, timeout: int = 7200) -> bool:
        req = urllib.request.Request(
            f"{self.base_url}/api/pull",
            data=json.dumps({"model": name, "stream": True}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                for raw in r:
                    if not raw.strip():
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event.get("error"):
                        if on_progress:
                            on_progress({"error": event["error"]})
                        return False
                    if on_progress:
                        on_progress(event)
            self._detail_cache.pop(name, None)
            return True
        except Exception as exc:
            if on_progress:
                on_progress({"error": str(exc)})
            return False

    def delete(self, name: str) -> bool:
        req = urllib.request.Request(
            f"{self.base_url}/api/delete",
            data=json.dumps({"model": name}).encode(),
            headers={"Content-Type": "application/json"}, method="DELETE",
        )
        try:
            urllib.request.urlopen(req, timeout=self.timeout)
            self._detail_cache.pop(name, None)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------- #

def all_gpu_vram_mb() -> list[tuple[int, int]]:
    """`(free, total)` VRAM in MiB for every card `nvidia-smi` reports, in index
    order, or `[]` when nvidia-smi is not reachable.

    `nvidia-smi --query-gpu` emits one row per card. This is the only place
    that reads every row; everything else picks a single card out of this
    list by index, because Ollama places a model on one card, not a pooled
    sum of all of them (BACKLOG `multi-gpu-vram-accounting`).
    """
    for cmd in (["nvidia-smi"], ["flatpak-spawn", "--host", "nvidia-smi"]):
        try:
            proc = subprocess.run(
                cmd + ["--query-gpu=memory.free,memory.total",
                       "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                cards = []
                for row in proc.stdout.strip().splitlines():
                    free, total = (int(v.strip()) for v in row.split(",")[:2])
                    cards.append((free, total))
                return cards
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    return []


def gpu_vram_mb(index: int = 0) -> tuple[int, int] | None:
    """`(free, total)` VRAM in MiB for one card, or None when nvidia-smi is not
    reachable or `index` names a card that is not present.

    `index` is the card Ollama will actually place a model on (`Config.gpu_index`,
    default 0 -- the only card on a single-GPU host). Summing across cards, or
    always reading row 0 regardless of `index`, judges the model against VRAM
    that may not be on the card it lands on -- wrong in both directions on a
    multi-GPU host: it can report free VRAM that is actually on a different,
    pinned card, or refuse a model that fits the target card fine because
    another idle card dragged a pooled total down.

    Free matters more than total here. The failure this project exists to catch
    is a *second* process holding the card -- the v1 slowdown was a host Ollama
    service on :11434 pinning qwen2.5:14b while the container on :12434 tried to
    load a 32b and silently offloaded 42 of 65 layers to an i7-6700K. Checking
    against total VRAM cannot see that; checking against free can.
    """
    cards = all_gpu_vram_mb()
    if not cards or index >= len(cards) or index < 0:
        return None
    return cards[index]


def config_gpu_indices(config: Any) -> list[int]:
    """The cards `config.base_url`'s Ollama server can place a model on:
    `gpu_indices` when set, else the single `gpu_index`."""
    indices = list(getattr(config, "gpu_indices", None) or [])
    return indices or [int(getattr(config, "gpu_index", 0))]


def gpu_free_mb_for(config: Any) -> int | None:
    """Free VRAM in MiB across the cards `config` says its Ollama server uses.

    One card: that card's free VRAM. Several (`gpu_indices`): the sum over
    exactly those cards, because Ollama splits a model too big for one card
    across every card it can see -- but never cards outside that set (a card
    pinned to another server is not ours). None if any listed card is
    missing or nvidia-smi is unreachable: an unknown is not a fit."""
    indices = config_gpu_indices(config)
    if len(indices) == 1:
        return gpu_free_mb(indices[0])
    cards = all_gpu_vram_mb()
    if not cards or any(i < 0 or i >= len(cards) for i in indices):
        return None
    return sum(cards[i][0] for i in sorted(set(indices)))


def gpu_free_mb(index: int = 0) -> int | None:
    """Free VRAM in MiB for one card, or None when nvidia-smi is not reachable."""
    vram = gpu_vram_mb(index)
    return None if vram is None else vram[0]


def model_need_gb(info: ModelInfo, num_ctx: int) -> float:
    """Estimated VRAM a model plus its KV cache needs, in GB. Shared by the fit
    check and the gpu-queue lease size, so both judge the same number."""
    # q4_0 KV cache, empirically ~0.9 GB per 64K context for this model class.
    return info.size_gb + (num_ctx / 65536) * 0.9


def gpu_free_mb_present(config: Any) -> int | None:
    """Free VRAM summed over the listed cards that actually exist. Unlike
    `gpu_free_mb_for` a missing card is skipped, not unknown: this answers
    "what is left if the second card is gone", for the fallback fit only.
    None when nvidia-smi is unreachable or no listed card exists."""
    cards = all_gpu_vram_mb()
    present = [i for i in sorted(set(config_gpu_indices(config))) if 0 <= i < len(cards)]
    if not present:
        return None
    return sum(cards[i][0] for i in present)


def choose_session_model(config: Any, installed: dict, reclaimable_mb: int = 0,
                         free_mb: int | None = None, present_mb: int | None = None,
                         probe: bool = True) -> str | None:
    """2.50.0: switch this session to `config.fallback_model` when `config.model`
    cannot fit, and say so. Returns the one line to print, or None (no change).

    The main model fails the check when it does not fit OR its VRAM is unknown
    (a listed card missing, nvidia-smi down). The fallback is used only when it
    is installed and provably fits the cards that are present; otherwise
    nothing changes and the usual fit warnings apply. The configured default
    is never overwritten: `Config.save` restores it (`_fallback_from`)."""
    fallback = getattr(config, "fallback_model", "") or ""
    primary = config.model
    if not fallback or fallback == primary:
        return None
    main_info, fb_info = installed.get(primary), installed.get(fallback)
    if main_info is None or fb_info is None:
        return None
    if probe:
        free_mb = gpu_free_mb_for(config)
        present_mb = gpu_free_mb_present(config)
    if free_mb is not None:
        fits, why = fit_report(main_info, config.num_ctx, free_mb, reclaimable_mb)
        if fits:
            return None
    else:
        why = f"VRAM unknown on cards {config_gpu_indices(config)} (a listed card is missing?)"
    if present_mb is None:
        return None
    fb_fits, fb_why = fit_report(fb_info, config.num_ctx, present_mb, reclaimable_mb)
    if not fb_fits:
        return None
    config._fallback_from = (primary, config.subagent_model)
    config.model = fallback
    if not config.subagent_model or config.subagent_model == primary:
        config.subagent_model = fallback
    return (f"Using fallback model {fallback} for this session: {primary} does not fit "
            f"({why}). Fallback fits: {fb_why}.")


def fit_report(info: ModelInfo, num_ctx: int, free_mb: int | None,
               reclaimable_mb: int = 0) -> tuple[bool, str]:
    """Whether a model plus its KV cache is expected to stay GPU-resident.

    `free_mb` is *free* VRAM, not total. `reclaimable_mb` is VRAM held by models
    this Ollama server will release on `unload_all()` -- that memory is ours to
    take, so it counts as available. VRAM held by anything else does not, which
    is what makes a second process on the card show up as a refusal instead of a
    silent CPU spill.

    Measured on a 24GB RTX 3090: qwen3.6:35b-a3b-coding (22.0GB) is fully
    resident at 65536 context and spills at 131072.
    """
    if free_mb is None:
        return True, "no GPU detected -- cannot check fit"
    need_gb = model_need_gb(info, num_ctx)
    have_gb = (free_mb + max(0, reclaimable_mb)) / 1024
    if need_gb <= have_gb - 0.4:
        detail = f"~{need_gb:.1f}GB of {have_gb:.1f}GB available VRAM"
        if reclaimable_mb > 0:
            detail += f" (incl. {reclaimable_mb / 1024:.1f}GB Wilbur will evict)"
        return True, detail
    msg = (f"~{need_gb:.1f}GB needed but only {have_gb:.1f}GB VRAM available -- "
           f"will spill to CPU (expect a large slowdown). ")
    held_elsewhere = reclaimable_mb == 0 and free_mb is not None
    if held_elsewhere:
        msg += ("If another process holds the card (a second Ollama on a different "
                "port is the usual cause) free it first. Otherwise lower num_ctx "
                "or pick a smaller model.")
    else:
        msg += "Lower num_ctx or pick a smaller model."
    return False, msg
