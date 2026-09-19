# Changelog

Earlier history lives in `git log`; this file starts at 2.4.0.


## 2.12.0

- Wilbur improvement programme, checkpoint 2: a `run_tests` tool that runs the project's
  own test command (pytest via JUnit XML, or `npm test`/`make test` with best-effort
  regex parsing) and returns pass/fail counts plus file:line detail per failure instead
  of raw test-runner output the model has to re-parse itself.
- A skills system: `SKILL.md` folders (a short frontmatter name/description the model
  sees up front, a body loaded only on demand) discovered from a project's
  `.wilbur/skills/`, the user's `~/.config/wilbur/skills/`, and three built-in starter
  skills (`writing-tests`, `bench-tasks`, `changelog-entries`), exposed via `list_skills`
  and `load_skill`.
- `bench/tasks/07_test_diagnosis` added to the task benchmark; validated the same way as
  the existing six tasks (see `bench/VALIDATION.md`).

## 2.11.0

- Wilbur improvement programme, checkpoint 1: a task benchmark harness (`bench/`) for
  measuring agent quality before/after a change against fixed coding tasks with real
  pass/fail tests, run headlessly and recorded to JSONL (pass rate, rounds, wall time,
  tokens/s). Tested only against the fake model backend so far; the live run needs a
  Wilbur GPU window (see `bench/README.md`).
- Tool-call recovery now repairs common local-model JSON mistakes before giving up: a
  trailing comma before `}`/`]`, and single-quoted strings in place of double (only when
  the text has no double quotes at all, so a legitimately mixed string is never
  mangled). Applies to bare/tagged/fenced calls and to a string-encoded `arguments`
  field alike.
- `OllamaClient` retries a transient failure (connection reset, timeout, 5xx) with
  capped exponential backoff (default 3 retries); a 4xx or any other error is never
  retried.

## 2.10.0

- Idle sign-off: when a turn ends with nothing left in flight (no subagents running, nothing queued, no round
  limit hit), Wilbur prints `THANK YOU FOR THE SLOP — MAY I HAVE ANOTHER?`. It comes from the REPL, not the
  model, so it is an honest "waiting on you" signal. Configure with `idle_signoff` / `idle_signoff_text`.

## 2.9.0

- `--sessions --json` emits the session listing as machine-readable JSON
  (id, cwd, updated, objective, turns), for the VS Code extension's
  session picker instead of parsing the human-readable table.

## 2.8.0

- The terminal look matches Claude Code's: prompt_toolkit replaces the
  hand-rolled fd-level stdin pump on a real tty. Output scrolls above a
  pinned input line with a bottom toolbar (spinner/status while a turn
  runs, then model, tokens and running subagents). History persists under
  `~/.config/wilbur/history`; Alt-Enter (Escape then Enter) inserts a
  newline for multi-line input; `/`-commands tab-complete. Off a tty
  (pipes, `WILBUR_NO_PTK=1`, or prompt_toolkit not installed) the old pump
  is used unchanged. Typing during a turn still queues a command, approval
  prompts still route through the same prompt, Ctrl-C still cancels a turn
  or subagents, Ctrl-D still exits.
- First-run model picker: if the configured model isn't installed, Wilbur
  lists the installed tool-capable models with a VRAM fit mark for the
  current GPU, lets you pick one, and saves it. If nothing tool-capable is
  installed, it suggests an `ollama pull` sized to detected free VRAM. If
  Ollama isn't reachable, it prints how to start it.
- The shipped default model is now `qwen3:14b`, a mainstream tool-capable
  model that a public release should work with out of the box.
- License, notice and brand files added (`LICENSE`, `NOTICE`, `BRAND.md`)
  ahead of the first public release.
- VS Code extension bumped to 0.2.0: `wilbur.path` defaults to `wilbur`,
  the `repository`/gallery fields point at the public
  `github.com/jsonholdings/wilbur` repo, a gallery banner was added, and
  the terminal tab now shows a full-colour Wilbur icon instead of the
  generic terminal icon.
- Each tool result shows its own elapsed time. `/help` is grouped into Model,
  Turn, Session and Other sections instead of one flat list.
- The GPU fit check now reads one card by index (`gpu_vram_mb(index)` /
  `gpu_free_mb(index)`, backed by a new `all_gpu_vram_mb()`) instead of always
  reading `nvidia-smi`'s first row regardless of which card Ollama would
  place a model on. `Config.gpu_index` (default 0) names the target card,
  ready for a second one.
- The system prompt carries a short set of always-on operating rules
  (verify and label claims, name explicit targets for destructive commands,
  stage only changed files, test before committing) alongside the identity
  lock, independent of persona.

## VS Code extension (`vscode/`)

A VS Code extension (own version, 0.2.0) that runs Wilbur in a terminal
panel — the shape of Claude Code's own VS Code integration. Commands:
`Wilbur: Open`, `Wilbur: Ask about selection` (`ctrl+alt+w`), `Wilbur: Send
current file`. See `vscode/README.md` and `vscode/CHANGELOG.md`. Does not
change the CLI's own version.

## 2.5.1

- Fix: text typed during a turn vanished as it was typed. The input pump
  echoed each key and then paused the spinner, and pausing clears the line.
  It now pauses once, before the first echoed key. Regression test drives a
  real pty with the spinner active.

## 2.5.0

- Typing is visible while a turn is running: the stdin pump reads at the fd
  level and switches a real terminal to cbreak mode so keystrokes echo as
  they're typed instead of being held by the kernel until Enter. Verified
  against a real pty, not just a pipe.
- Subagents get their own tool-round budget (`subagent_max_turns`) instead of
  inheriting the parent's `max_turns`.
- `/agents` lists in-flight and recently finished subagents (role, status,
  elapsed time, current tool). The spinner shows a one-line "(N subagents
  running)" status while any are live.
- Ctrl-C now reaches a running subagent: a cooperative cancel flag is checked
  between tool rounds and before each tool call, so an interrupted subagent
  stops instead of running unattended in the background.

## 2.4.0

- Hitting the tool-round limit no longer throws the turn's work away. Wilbur
  makes one final no-tools model call asking what was done, what is left, and
  the next concrete step, and shows that instead of the bare "stopped after
  N rounds" notice. The transcript is untouched by that call.
- `/continue` (or typing "continue") resumes a task with a fresh round budget
  and the same context.
- `/turns [n]` shows or sets the tool-round limit (`max_turns`) at runtime.
- The system prompt now tells the model to split a long task into small,
  numbered, independently-testable chunks and verify each one before moving
  on.
