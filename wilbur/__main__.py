"""Entry point: `wilbur` for a session, `wilbur -p '...'` for one-shot."""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__, ui
from .agent import Agent
from .config import CONFIG_PATH, Config
from .models import ModelManager, fit_report, gpu_free_mb
from .repl import Repl
from .tools.base import Approval

# Sized to the tiers a normal desktop/workstation GPU falls into. Every name
# here is mainstream (not an abliterated/finetuned variant) and declares Ollama
# tool-calling support, so a first-time user's `/pull` actually produces a
# working agent.
PULL_SUGGESTIONS = (
    (8, "qwen2.5:7b"),
    (16, "qwen3:14b"),
    (float("inf"), "qwen3:32b"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wilbur", description="Local coding agent for the terminal.")
    parser.add_argument("prompt", nargs="*", help="run this prompt and exit")
    parser.add_argument("-p", "--print", dest="oneshot", metavar="PROMPT",
                        help="run a single prompt non-interactively and print the result")
    parser.add_argument("-m", "--model", help="model to use for this run")
    parser.add_argument("-c", "--context", type=int, help="context window in tokens")
    parser.add_argument("-C", "--cwd", default=os.getcwd(), help="working directory")
    parser.add_argument("--url", help="Ollama base URL")
    parser.add_argument("--yes", action="store_true",
                        help="approve every tool call without prompting")
    parser.add_argument("--list-models", action="store_true",
                        help="list local models with their agent suitability")
    parser.add_argument("--continue", dest="continue_", action="store_true",
                        help="resume the most recent session in this directory")
    parser.add_argument("--resume", metavar="ID",
                        help="resume a session by id (see --sessions)")
    parser.add_argument("--sessions", action="store_true",
                        help="list saved sessions")
    parser.add_argument("--forget", metavar="ID",
                        help="delete a saved session by id, so --continue/--resume "
                             "can never pick it up again (see --sessions for ids)")
    parser.add_argument("--json", action="store_true",
                        help="with --sessions, emit machine-readable JSON")
    parser.add_argument("--version", action="version", version=f"wilbur {__version__}")
    args = parser.parse_args(argv)

    config = Config.load()
    if args.url:
        config.base_url = args.url
    if args.model:
        config.model = args.model
        config.subagent_model = args.model
    if args.context:
        config.num_ctx = args.context

    if args.list_models:
        return _list_models(config)

    prompt = args.oneshot or (" ".join(args.prompt) if args.prompt else "")
    if prompt:
        return _oneshot(config, args.cwd, prompt, auto=args.yes or bool(args.oneshot))

    if args.forget:
        from . import session as sessions
        if sessions.forget(args.forget):
            print(f"forgot session {args.forget}")
            return 0
        print(f"no saved session {args.forget}")
        return 1

    if args.sessions:
        from . import session as sessions
        rows = sessions.listing()
        if args.json:
            import json as _json
            print(_json.dumps(rows))
            return 0
        if not rows:
            print("no saved sessions")
            return 0
        import time as _time
        for r in rows:
            when = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(r["updated"]))
            obj = (r["objective"] or [""])[0][:60]
            print(f"  {r['id']:<34} {when}  {r['turns']:>4} msgs  {obj}")
        return 0

    restore = None
    if args.resume or args.continue_:
        from . import session as sessions
        restore = (sessions.load(args.resume) if args.resume
                   else sessions.latest(args.cwd))
        if restore is None:
            # Refusing is right: silently starting fresh when the user asked to
            # resume is how work gets repeated or lost.
            target = args.resume or f"directory {args.cwd}"
            print(f"no saved session for {target}. --sessions lists what exists.")
            return 1

    rc = _ensure_model(config)
    if rc is not None:
        return rc

    repl = Repl(config, args.cwd, restore=restore)
    if args.yes:
        repl.approve_mode = "all"
    return repl.run()


def _pull_suggestion(free_mb: int | None) -> str:
    if free_mb is None:
        return "qwen3:14b"  # no GPU detected -- the safe middle tier, not the smallest
    gb = free_mb / 1024
    for ceiling, name in PULL_SUGGESTIONS:
        if gb <= ceiling:
            return name
    return PULL_SUGGESTIONS[-1][1]  # pragma: no cover -- unreachable, last tier is inf


def _ensure_model(config: Config) -> int | None:
    """Make sure `config.model` is actually usable before the REPL starts.

    Returns an exit code if the process should stop here (Ollama unreachable,
    or nothing tool-capable is installed yet), or None once `config.model`
    names something installed -- picking it interactively and saving it first
    if the configured default isn't there.
    """
    manager = ModelManager(config.base_url)
    if not manager.available():
        print(ui.error(f"No Ollama reachable at {config.base_url}."))
        print(ui.notice(
            "Start it first, e.g.:\n"
            "    ollama serve        # or: systemctl --user start ollama\n"
            "Then run `wilbur` again."))
        return 1

    installed = {m.name: m for m in manager.list_models()}
    if config.model in installed and installed[config.model].supports_tools:
        return None

    capable = sorted((m for m in installed.values() if m.supports_tools),
                      key=lambda m: m.name)
    free_mb = gpu_free_mb()

    if not capable:
        suggestion = _pull_suggestion(free_mb)
        vram_note = "no GPU detected" if free_mb is None else f"~{free_mb / 1024:.0f}GB free VRAM"
        print(ui.notice(
            f"No tool-capable model is installed yet ({vram_note}). "
            f"Configured default ({config.model}) isn't installed either."))
        print(ui.notice(f"\n    ollama pull {suggestion}\n"))
        print(ui.notice("Run that, then start `wilbur` again."))
        return 1

    reclaimable = manager.reclaimable_vram_mb()
    print(ui.notice(f"The configured model ({config.model}) isn't installed. Pick one:\n"))
    for i, m in enumerate(capable, 1):
        fits, why = fit_report(m, config.num_ctx, free_mb, reclaimable)
        tag = ui.c("ok      ", ui.GREEN) if fits else ui.c("degraded", ui.RED)
        print(f"  {i:>2}. {tag}  {ui.c(m.name, ui.BOLD):<48} {m.size_gb:5.1f}GB  "
              f"{ui.c(why, ui.GREY)}")

    picked = _prompt_choice(capable)
    config.model = picked.name
    if not config.subagent_model or config.subagent_model not in installed:
        config.subagent_model = picked.name
    config.save()
    print(ui.success(f"\nUsing {picked.name}. Saved to {CONFIG_PATH}."))
    return None


def _prompt_choice(capable: list):
    while True:
        try:
            choice = input(f"\nPick a model [1-{len(capable)}]: ").strip()
        except EOFError:
            choice = ""
        if not choice:
            continue
        try:
            idx = int(choice)
        except ValueError:
            print("  enter a number from the list above")
            continue
        if 1 <= idx <= len(capable):
            return capable[idx - 1]
        print(f"  enter a number from 1 to {len(capable)}")


def _list_models(config: Config) -> int:
    manager = ModelManager(config.base_url)
    if not manager.available():
        print(ui.error(f"No Ollama at {config.base_url}."))
        return 1
    vram = gpu_free_mb(config.gpu_index)
    reclaimable = manager.reclaimable_vram_mb()
    # "VRAM" alone read as total before the fit check moved to free VRAM; both
    # numbers matter here, since the gap between them is another process.
    if vram is None:
        vram_note = "VRAM ?"
    else:
        vram_note = f"VRAM {vram:,} MiB free"
        if reclaimable:
            vram_note += f" + {reclaimable:,} reclaimable"
    print(ui.notice(f"context {config.num_ctx:,} tokens · "
                    f"{vram_note} · {config.base_url}\n"))
    for m in manager.list_models():
        fits, why = fit_report(m, config.num_ctx, vram, reclaimable)
        if not m.supports_tools:
            tag, note = ui.c("unusable", ui.RED), "no tool-calling support"
        elif not fits:
            tag, note = ui.c("degraded", ui.RED), why
        else:
            tag, note = ui.c("ok      ", ui.GREEN), why
        mark = " ←" if m.name == config.model else ""
        print(f"  {tag}  {ui.c(m.name, ui.BOLD):<48} {m.size_gb:5.1f}GB  "
              f"{ui.c(note, ui.GREY)}{mark}")
    return 0


def _oneshot(config: Config, cwd: str, prompt: str, auto: bool) -> int:
    agent = Agent(
        config, cwd,
        approve=lambda tool, detail: Approval.GRANTED if auto else Approval.DENIED,
        on_event=_print_event,
    )
    try:
        reply = agent.run(prompt)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(ui.error(f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1
    print(reply)
    return 0


def _print_event(kind: str, data: dict) -> None:
    if kind == "tool_start":
        args = data["args"]
        summary = (args.get("command") or args.get("pattern")
                   or args.get("path") or "")
        print(ui.tool_call(data["name"], str(summary)[:70], data.get("recovered")),
              file=sys.stderr)
    elif kind == "tool_end":
        print(ui.tool_result(data.get("output", ""), data.get("error", False)),
              file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
