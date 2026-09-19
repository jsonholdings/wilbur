"""Runtime configuration for Wilbur."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("WILBUR_CONFIG", Path.home() / ".config" / "wilbur" / "config.json"))
SESSION_DIR = Path(os.environ.get("WILBUR_SESSIONS", Path.home() / ".local" / "share" / "wilbur" / "sessions"))
# prompt_toolkit's FileHistory for the pinned-bottom UI (wilbur.repl._ptk_pump).
# Kept alongside CONFIG_PATH/SESSION_DIR, env-overridable for the same reason:
# a test that exercises the real pump must not write into the real home dir.
HISTORY_PATH = Path(os.environ.get("WILBUR_HISTORY", Path.home() / ".config" / "wilbur" / "history"))


@dataclass
class Config:
    # --- model / endpoint ---
    base_url: str = "http://localhost:11434"
    # Which card `nvidia-smi`'s per-row output the fit check reads -- the card
    # Ollama's `base_url` server will actually place a model on. 0 is the only
    # card on a single-GPU host; set alongside `base_url` when a second card
    # (or a second Ollama endpoint bound to it) is added.
    gpu_index: int = 0
    # Mainstream, tool-capable, and widely pulled -- a public release's shipped
    # default should work for someone who has never heard of this project's
    # abliterated-model history. `wilbur/__main__.py`'s first-run picker
    # catches the case where this isn't actually installed and offers what is.
    model: str = "qwen3:14b"
    subagent_model: str = ""          # falls back to `model`
    temperature: float = 0.15
    top_p: float = 0.9

    # --- context budget ---
    # 64K is the largest window that stays fully GPU-resident on a 24GB card
    # with q4_0 KV cache. Going past it silently spills layers to CPU and
    # costs roughly 5x throughput, so it is a hard ceiling, not a suggestion.
    num_ctx: int = 65536
    compact_at: float = 0.75          # begin compaction at 75% of num_ctx
    reserve_output: int = 4096        # never let input crowd out the reply

    # --- tool output limits ---
    # A single `cat` of a large file can consume an entire local context
    # window. Every tool result is clamped before it reaches the model.
    max_tool_output_chars: int = 24000
    max_tool_output_lines: int = 800
    max_read_lines: int = 1500

    # --- agent loop ---
    max_turns: int = 60               # tool-call rounds per user message
    parallel_tools: bool = False      # local models are unreliable at this
    request_timeout: int = 600
    # A `task` subagent runs synchronously inside one of the parent's tool
    # calls, so nothing else bounds its wall time: max_turns * request_timeout
    # is its theoretical ceiling (up to 10h) with zero visibility to the user
    # in the meantime, which reads as "never returns". This is the hard cap
    # actually enforced, independent of the model finishing a turn.
    subagent_timeout_s: int = 300
    # A subagent's own tool-round budget, independent of the parent's
    # `max_turns`. Sharing the parent's (often much larger) budget let a
    # runaway subagent burn the wall-clock timeout above without ever
    # hitting a round limit that would make it stop and summarise instead.
    subagent_max_turns: int = 25

    # Printed by the REPL (not the model) when a turn ends and nothing is left in
    # flight: no subagents running, nothing queued, no round limit hit.
    idle_signoff: bool = True
    idle_signoff_text: str = "THANK YOU FOR THE SLOP \u2014 MAY I HAVE ANOTHER?"

    # --- team mode ---
    # "auto" sizes the gate itself (wilbur.team.needs_team): a trivial,
    # single-file ask runs single-agent; a multi-file or risky one gets the
    # plan -> code -> test -> review -> merge team. "on"/"off" override it.
    team_mode: str = "auto"
    # Empty means the reviewer uses `model` too. If set, it is only used when
    # it fits free VRAM at review time (wilbur.team._reviewer_model), with a
    # logged fallback to `model` otherwise -- the GPU may be shared with
    # other local processes.
    reviewer_model: str = ""
    team_max_revisions: int = 2       # denial loops before asking the user
    team_role_timeout_s: int = 0      # 0 -> falls back to subagent_timeout_s
    team_reviewer_min_free_mb: int = 8192

    # --- voice ---
    # "wilbur" for the Appalachian voice from the v1 SOUL.md, "none" for plain
    # prose. The identity lock (prompt-injection resistance) applies either way
    # -- it is a security property, not a character trait.
    persona: str = "wilbur"

    # --- permissions ---
    auto_approve: list[str] = field(default_factory=lambda: [
        "read_file", "list_files", "glob", "grep", "todo_write",
    ])
    denied_bash: list[str] = field(default_factory=lambda: [
        "rm -rf /", "mkfs", "dd if=", ":(){", "shutdown", "reboot",
    ])

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text())
            for key, value in data.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
        if not cfg.subagent_model:
            cfg.subagent_model = cfg.model
        return cfg

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2))
