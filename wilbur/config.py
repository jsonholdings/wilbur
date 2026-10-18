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
# User-scope SKILL.md folders (see wilbur/skills.py), same env-override
# pattern as the paths above so tests can redirect it without touching the
# owner's real ~/.config/wilbur/skills.
USER_SKILLS_DIR = Path(os.environ.get("WILBUR_USER_SKILLS_DIR", Path.home() / ".config" / "wilbur" / "skills"))


_NOT_SAVED = ("backend", "fake_script")


@dataclass
class Config:
    # --- model / endpoint ---
    base_url: str = "http://localhost:11434"
    # "ollama" (the real thing) or "fake": scripted replies from `fake_script`
    # with no model at all (wilbur.fake_backend), for tests and scripted runs.
    # Never chosen automatically -- only by setting it here or --backend.
    backend: str = "ollama"
    fake_script: str = ""
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
    # Bounded message check (wilbur.inbox): a line typed while a turn runs
    # is read within this many seconds -- between tool rounds or
    # pre-empting a sleep/wait command -- and `wilbur status
    # --check` fails on one left unread past 2x this.
    message_check_max_s: float = 60.0
    request_timeout: int = 600
    # Longest silence (no bytes) from the model server before Wilbur reports
    # "model server not responding" and offers /retry, instead of waiting out
    # request_timeout. Covers model load + prompt reading before the first
    # token (~60 s for 64k tokens at 1,100 tok/s on this card).
    first_token_timeout_s: int = 120
    # A `task` subagent runs synchronously inside one of the parent's tool
    # calls, so nothing else bounds its wall time: max_turns * request_timeout
    # is its theoretical ceiling (up to 10h) with zero visibility to the user
    # in the meantime, which reads as "never returns". This is the hard cap
    # actually enforced, independent of the model finishing a turn.
    subagent_timeout_s: int = 300
    constrained_tool_calls: bool = False  # opt-in: Ollama `format` schema constrains tool calls
    critic_pass: bool = False         # opt-in: one extra model call reviews each write
    image_generation: bool = False    # opt-in generate_image tool (runs through gpu-queue)
    image_model: str = "qwen-image-2512"  # or "qwen-image-2.1" (research-only licence)
    # A subagent's own tool-round budget, independent of the parent's
    # `max_turns`. Sharing the parent's (often much larger) budget let a
    # runaway subagent burn the wall-clock timeout above without ever
    # hitting a round limit that would make it stop and summarise instead.
    subagent_max_turns: int = 25

    # Printed by the REPL (not the model) when a turn ends and nothing is left in
    # flight: no subagents running, nothing queued, no round limit hit.
    idle_signoff: bool = True
    idle_signoff_text: str = "TASK COMPLETE \u2014 CAN YOU SLOP THE TROUGH?"

    # --- team mode ---
    # "auto" sizes the gate itself (wilbur.team.needs_team): a trivial,
    # single-file ask runs single-agent; a multi-file or risky one gets the
    # plan -> code -> test -> review -> merge team. "on"/"off" override it.
    team_mode: str = "auto"
    # /loop <goal>: loop turns and wall clock before Wilbur stops and reports.
    loop_max_turns: int = 20
    loop_max_minutes: int = 60
    # Keep going (wilbur.keepgoing): a plain prompt whose turn ends with the
    # model's own plan still open, or a "Next, I'll ..." with no tool call, is
    # nudged on automatically. A question to the user, an approval that was
    # refused, Ctrl-C, or a stuck run stops it. Off: auto_continue false, or
    # /keepgoing off.
    #
    # The old 10-continue / 30-minute caps are gone as the default stop (owner,
    # 2026-10-02: "if I tell him to work all night, he should do it"). They are
    # still honoured when set > 0, for a user who wants a hard numeric limit;
    # 0 means no cap and the run horizon + stuck detector bound it instead.
    auto_continue: bool = True
    auto_continue_max: int = 0            # 0 = no continue-count cap
    auto_continue_max_minutes: int = 0    # 0 = no keep-going wall-clock cap

    # Run horizon (wilbur.horizon): how long a long run may go. The default is
    # until the task's todo plan is complete; a horizon the user states ("all
    # night", "for 3 hours", "until done") overrides it; the safety maximum
    # bounds even that. The stuck detector is the real safety net -- it stops
    # a spinning run after this many consecutive turns with no progress.
    run_safety_max_minutes: int = 12 * 60
    stuck_limit: int = 5
    # Never accept "done" without evidence (wilbur.doneclaim): a completion
    # claim with plan items open, or "tests pass" with no test run, makes the
    # harness run the project's own check -- pass ticks the plan and stops as
    # COMPLETE, fail sends a specific nudge.
    verify_done_claims: bool = True

    # Durability (wilbur.repl long runs): commit a tested checkpoint to the
    # current git branch every N auto-continues (never push), and write a
    # progress log the user can read in the morning. 0 disables checkpoint
    # commits.
    checkpoint_commit_every: int = 10
    progress_log: bool = True
    # Empty means the reviewer uses `model` too. If set, it is only used when
    # it fits free VRAM at review time (wilbur.team._reviewer_model), with a
    # logged fallback to `model` otherwise -- the GPU may be shared with
    # other local processes.
    reviewer_model: str = ""
    team_max_revisions: int = 2       # denial loops before asking the user
    team_role_timeout_s: int = 0      # 0 -> falls back to subagent_timeout_s
    team_reviewer_min_free_mb: int = 8192

    # --- stopped-state visibility (wilbur.runstate) ---
    # Bell (\a) and terminal title (OSC 0) on entering `stopped`.
    # Default on; off when stdout is not a tty (checked at call time).
    stop_bell: bool = True
    set_title: bool = True
    # `wilbur status --check`: a `working` run with no progress in this many
    # minutes is considered hung (exit 1). 0 = no hung detection.
    status_hung_minutes: int = 30
    # Optional idle exit: after a horizon-stop, if no input arrives within
    # this many minutes, save the session and exit. 0 = off (default).
    # Interactive sessions without a horizon are never auto-exited.
    idle_exit_minutes: int = 0

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
        data = asdict(self)
        # The backend is never persisted from a running session: a one-off
        # `--backend fake` followed by /context or /model must not leave the
        # config on the fake for every later run. Only a hand edit changes it.
        on_disk = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        for key in _NOT_SAVED:
            if key in on_disk:
                data[key] = on_disk[key]
            else:
                data.pop(key, None)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(data, indent=2))
