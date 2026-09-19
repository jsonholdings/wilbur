"""The models Wilbur knows about.

Three groups, and the distinctions matter:

CORE is what a plain install brings down. ALTERNATES are measured here but no
longer installed by default. EXTENDED is a broader catalog of tool-capable and
general-purpose models worth knowing about, kept so `/search` and the
first-run picker can point at something even when it isn't installed by
default. Most of EXTENDED cannot run well on a 24GB card and none of it was
measured here -- each entry says so.

`install.sh` reads this module at run time, so the installer and the docs cannot
drift. `wilbur/registry.py` searches it, so `/search` works offline.

SIZES IN `EXTENDED` ARE PUBLISHERS' APPROXIMATE FOOTPRINTS AT THE DEFAULT
QUANTISATION, NOT MEASUREMENTS TAKEN HERE. Only entries with `measured=True`
carry a number this project actually observed on its own card.
"""
from __future__ import annotations

from dataclasses import dataclass

# Ollama's own default port, and the one a normal install listens on. An
# alternate port some Compose-based Ollama setups publish instead.
DEFAULT_OLLAMA_PORT = 11434
LEGACY_CONTAINER_PORT = 12434
PROBE_PORTS = (DEFAULT_OLLAMA_PORT, LEGACY_CONTAINER_PORT)


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    gib: float
    role: str                 # default | alternate | superseded | extended
    origin: str               # what kind of task this model is best suited to
    note: str
    measured: bool = False    # True only where this project benchmarked it

    @property
    def required(self) -> bool:
        """Only the default is required.

        A machine that runs out of disk part-way through several hundred GB must
        still end up with a Wilbur that works.
        """
        return self.role == "default"     # only the one Wilbur falls back to

    @property
    def fits_24gb(self) -> bool:
        """Whether it stays GPU-resident on a 24GB card with room for context."""
        return self.gib <= 21.0


# Installed by default. Fitting a 24GB card is the selection criterion, not a
# nice-to-have: a model that does not fit runs on CPU at roughly 1-2% of GPU
# speed, which is indistinguishable from broken.
CORE: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        "qwen3:14b", 9.3, "default", "general",
        "Mainstream, tool-capable, widely pulled -- works without prior "
        "knowledge of any of this project's other model choices. Comfortably "
        "fits a 24GB card with plenty of headroom for context.", measured=True,
    ),
    CatalogEntry(
        "qwen2.5-coder:14b", 9.0, "default-alt", "coding",
        "Coding-focused sibling of the default at the same footprint, for "
        "when the task is mostly source-code editing rather than general "
        "reasoning.",
    ),
)

# Measured here, but no longer installed by default -- the default set is the
# two above.
ALTERNATES: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        "qwen3:32b", 20.0, "alternate", "general",
        "Larger, stronger reasoning. Fits a bare 24GB card, but leaves little "
        "headroom for context -- pull it by name if the extra reasoning is "
        "worth the risk of spilling to CPU.",
    ),
    CatalogEntry(
        "qwen2.5-coder:32b", 19.9, "alternate", "coding",
        "Larger coding model. Fits a bare 24GB card with little headroom.",
    ),
)

# A broader catalog of tool-capable and general-purpose models, for `/search`
# and the first-run picker's suggestions when nothing installed fits.
EXTENDED: tuple[CatalogEntry, ...] = (
    CatalogEntry("nomic-embed-text", 0.3, "extended", "embedding",
                 "Small embedding model for retrieval/RAG use cases."),
    CatalogEntry("qwen2.5:0.5b", 0.4, "extended", "general", "Smallest of the qwen2.5 ladder."),
    CatalogEntry("qwen2.5:3b", 1.9, "extended", "general", "Small general model."),
    CatalogEntry("qwen2.5:7b", 4.7, "extended", "general", "Mid-sized general model."),
    CatalogEntry("qwen2.5:14b", 9.0, "extended", "general", "Larger general model."),
    CatalogEntry("qwen2.5:32b", 20.0, "extended", "general", "Largest of the qwen2.5 general ladder."),
    CatalogEntry("qwen2.5-coder:7b", 4.7, "extended", "coding", "Small coding model."),
    CatalogEntry("deepseek-coder:6.7b", 3.8, "extended", "coding", "Compact coding model."),
    CatalogEntry("llama3.2:3b", 2.0, "extended", "general", "Small, fast, general model."),
    CatalogEntry("llama3.1:8b", 4.7, "extended", "general", "General chat model."),
    CatalogEntry("llama3.1:70b-instruct-q4_K_M", 43.0, "extended", "general",
                 "Large chat model. Will NOT fit a 24GB card -- expect heavy CPU spill."),
    CatalogEntry("llama3.3:70b", 43.0, "extended", "general",
                 "70B. Will NOT fit a 24GB card -- expect heavy CPU spill."),
    CatalogEntry("codellama:34b", 19.0, "extended", "coding", "Older dedicated coding model."),
    CatalogEntry("deepseek-r1:32b", 20.0, "extended", "reasoning", "Reasoning-tuned model."),
    CatalogEntry("mistral:7b-instruct", 4.1, "extended", "general", "Small general instruct model."),
)

# Named elsewhere but deliberately NOT pullable from a registry. Recorded
# rather than dropped, so nobody re-derives the exclusion later and re-adds a
# broken entry.
NOT_PULLABLE = {
    "coder-expert-70b": (
        "A locally-built model name (built from a Modelfile with `ollama "
        "create`); it does not exist in any registry, so `ollama pull "
        "coder-expert-70b` fails. Build it yourself from a Modelfile instead."
    ),
}

MODELS: tuple[CatalogEntry, ...] = CORE + ALTERNATES + EXTENDED
BY_NAME = {m.name: m for m in MODELS}
DEFAULT_MODEL = next(m.name for m in CORE if m.role == "default")

DEFAULT_GIB = sum(m.gib for m in CORE)
ALTERNATES_GIB = sum(m.gib for m in ALTERNATES)
CORE_GIB = DEFAULT_GIB + ALTERNATES_GIB
EXTENDED_GIB = sum(m.gib for m in EXTENDED)
TOTAL_GIB = CORE_GIB + EXTENDED_GIB
STAGING_HEADROOM_GIB = 10.0
REQUIRED_DISK_GIB = TOTAL_GIB + STAGING_HEADROOM_GIB
CORE_DISK_GIB = CORE_GIB + STAGING_HEADROOM_GIB
DEFAULT_DISK_GIB = DEFAULT_GIB + STAGING_HEADROOM_GIB


def model_names(include: str = "all") -> list[str]:
    """Names to pull, in pull order.

    "required" -> just the one Wilbur cannot run without
    "default"  -> what a plain install brings down (all fit a 24GB card)
    "core"     -> the above plus the measured alternates
    "all"      -> core first, then the extended catalog
    """
    if include == "required":
        return [m.name for m in CORE if m.required]
    if include == "default":
        return [m.name for m in CORE]
    if include == "core":
        return [m.name for m in CORE + ALTERNATES]
    return [m.name for m in MODELS]


def search(query: str) -> list[CatalogEntry]:
    """Case-insensitive match over name, origin and note."""
    q = query.strip().lower()
    if not q:
        return list(MODELS)
    return [m for m in MODELS
            if q in m.name.lower() or q in m.note.lower() or q in m.origin.lower()]
